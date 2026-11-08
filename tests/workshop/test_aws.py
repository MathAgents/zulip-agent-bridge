"""`workshop/aws/launch.py` and `teardown.py` (workshop/aws/README.md; the first boot,
workshop/ami/README.md) against a fake EC2."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
import types
import urllib.request

import aws_common as ac
import launch
import protect
import pytest
import teardown

from .scope import FLEET_SCOPE, with_scope


@pytest.fixture(autouse=True)
def never_sleep(monkeypatch):
    """`FakeEc2` shows and enforces every change at once, so no protect.py or teardown.py run
    here may wait: one that would (#378's read-back or retries) fails at once, rather than
    sleeping for 300 s. tests/workshop/test_protect_lag.py has the lag, on its own clock."""

    def sleep(seconds):
        raise AssertionError(f"a script slept {seconds} s against a fake EC2 without lag")

    fake = types.SimpleNamespace(sleep=sleep, monotonic=time.monotonic)
    for script in (protect, teardown):
        monkeypatch.setattr(script, "time", fake)


ROSTER = """github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes
alice,Alice,O'Neil,alice@example.org,alice/thesis,,,,,
bob-2,Bob,"Smith; rm -rf /",bob@example.org,sandbox,4242,,,,late
"""

#: Extra column, a row without a login, and a reordered header: all must survive a write-back.
ROSTER_MESSY = """notes,github,shirt,first_name,last_name,repo,github_id
,alice,M,Alice,O'Neil,thesis,
helper without a VM,,L,Hilda,Helper,,
late,bob-2,S,Bob,Smith,sandbox,4242
"""

GOOD_SG = {
    "GroupId": "sg-1",
    "GroupName": "zab-workshop",
    "IpPermissions": [
        {
            "IpProtocol": "tcp",
            "FromPort": 22,
            "ToPort": 22,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
        }
    ],
}


def console_text(*fingerprints: str) -> str:
    """A console as AMI v3 prints it (read with get_console_output(Latest=True), Oct 5)."""
    lines = [f"<14>Oct  5 10:52:31 cloud-init: 256 {fp} root@ip-172-31-5-215 (ED25519)"
             for fp in fingerprints]  # fmt: skip
    return (
        "[   57.1] cloud-init[1053]: Cloud-init v. 26.1 running 'modules:final'\n"
        "<14>Oct  5 10:52:31 cloud-init: -----BEGIN SSH HOST KEY FINGERPRINTS-----\n"
        + "\n".join(lines)
        + "\n<14>Oct  5 10:52:31 cloud-init: -----END SSH HOST KEY FINGERPRINTS-----\n"
        "-----BEGIN SSH HOST KEY KEYS-----\nssh-ed25519 AAAAC3Nza root@ip\n"
        "-----END SSH HOST KEY KEYS-----\n"
    )


class FakeEc2:
    def __init__(self, existing: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple] = []
        self.instances: dict[str, dict] = dict(existing or {})
        self.addresses: dict[str, dict] = {}
        self.security_groups: dict[str, dict] = {"sg-1": GOOD_SG}
        self._n = 0
        self.consoles: dict[str, list[str]] = {}  # instance id -> outputs, one per read
        self.console_reads: list[str] = []
        self.attributes: dict[str, list] = {}  # instance id -> [protected, shutdown behavior]
        self.broken: set[str] = set()  # instance ids whose attribute reads fail

    def console_output(self, iid):
        """What cloud-init printed: by default the block at once (AMI v3's format)."""
        self.console_reads.append(iid)
        outputs = self.consoles.get(iid)
        if outputs is None:
            return console_text(f"SHA256:fp{iid.replace('-', '')}")
        return outputs.pop(0) if len(outputs) > 1 else outputs[0]

    def find_instances(self, tags, states):
        self.calls.append(("find_instances", tags, states))
        out = []
        for inst in self.instances.values():
            t = ac.tags_of(inst)
            if all(t.get(k) == v for k, v in tags.items()) and inst["State"]["Name"] in states:
                out.append(inst)
        return out

    def find_addresses(self, tags):
        self.calls.append(("find_addresses", tags))
        return [
            a
            for a in self.addresses.values()
            if all(ac.tags_of(a).get(k) == v for k, v in tags.items())
        ]

    def describe_security_group(self, group_id):
        self.calls.append(("describe_security_group", group_id))
        if group_id not in self.security_groups:
            raise ValueError(f"security group {group_id} not found")
        return self.security_groups[group_id]

    def run_instance(self, **params):
        self._n += 1
        iid = f"i-{self._n:04d}"
        self.calls.append(("run_instance", params))
        self.instances[iid] = {
            "InstanceId": iid,
            "State": {"Name": "running"},
            "Tags": params["TagSpecifications"][0]["Tags"],
        }
        self.attributes[iid] = [
            bool(params.get("DisableApiTermination", False)),
            params.get("InstanceInitiatedShutdownBehavior", "stop"),
        ]
        return iid

    def protection(self, iid):
        self.calls.append(("protection", iid))
        if iid in self.broken:
            raise RuntimeError("UnauthorizedOperation")
        protected, behavior = self.attributes.setdefault(iid, [False, "stop"])
        return protected, behavior

    def termination_protection(self, iid):
        """At once, unlike EC2 (#378: tests/workshop/test_protect_lag.py has the lag)."""
        return self.protection(iid)[0]

    def set_termination_protection(self, iid, on):
        self.calls.append(("set_termination_protection", iid, on))
        self.attributes[iid][0] = on

    def set_shutdown_stop(self, iid):
        self.calls.append(("set_shutdown_stop", iid))
        self.attributes[iid][1] = "stop"

    def wait_running(self, iid):
        self.calls.append(("wait_running", iid))

    def allocate_address(self, tags):
        self._n += 1
        aid = f"eipalloc-{self._n:04d}"
        self.calls.append(("allocate_address", tags))
        self.addresses[aid] = {
            "AllocationId": aid,
            "PublicIp": f"203.0.113.{self._n}",
            "Tags": tags,
        }
        return aid

    def associate_address(self, aid, iid):
        self.calls.append(("associate_address", aid, iid))
        self.addresses[aid]["InstanceId"] = iid  # describe_addresses reports the association
        return self.addresses[aid]["PublicIp"]

    def terminate(self, ids):
        self.calls.append(("terminate", list(ids)))
        for i in ids:
            self.instances[i]["State"]["Name"] = "shutting-down"

    def wait_terminated(self, ids):
        self.calls.append(("wait_terminated", list(ids)))

    def release_address(self, aid):
        self.calls.append(("release_address", aid))
        del self.addresses[aid]


def opts(**kw) -> argparse.Namespace:
    base = {
        "ami": "ami-123",
        "instance_type": launch.INSTANCE_TYPE,
        "key_name": "drew",
        "security_group_id": "sg-1",
        "subnet_id": None,
        "only": None,
        "dry_run": False,
        "spares": 0,
        "tag": ["workshop=nt-agents-2026"],  # required since #808
    }
    base.update(kw)
    return argparse.Namespace(**base)


def quiet(*a, **k):
    pass


ROW = {"slug": "alice", "email": "alice@example.org", "repo": "thesis", "ip": "", "host": ""}


def fresh_row():
    return dict(ROW)


# ------------------------------------------------------------------------------------ roster


def test_roster_round_trip(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    rows = ac.read_roster(str(path))
    assert [r["github"] for r in rows] == ["alice", "bob-2"]
    assert rows[0]["github_id"] == "" and rows[1]["github_id"] == "4242"
    rows[0]["ip"] = "203.0.113.7"
    rows[0]["host"] = "ws-alice"
    ac.write_roster(str(path), rows)
    text = path.read_text()
    # the file's own column order, then the columns it lacked (slug written back,
    # workshop/admin/README.md §4)
    old = ROSTER.splitlines()[0].split(",")
    assert text.splitlines()[0] == ",".join([*old, *[c for c in ac.ROSTER_COLUMNS if c not in old]])
    assert [r["slug"] for r in ac.read_roster(str(path))] == ["alice", "bob-2"]
    again = ac.read_roster(str(path))
    assert again[0]["ip"] == "203.0.113.7" and again[1]["notes"] == "late"
    assert (tmp_path / "roster.csv.bak").read_text() == ROSTER
    assert not (tmp_path / "roster.csv.tmp").exists()


def test_roster_write_back_keeps_every_row_and_column(tmp_path):
    """Unknown columns, the file's own column order and rows without a login all survive."""
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER_MESSY)
    rows = ac.read_roster(str(path))
    assert len(rows) == 3 and [r["github"] for r in ac.participants(rows)] == ["alice", "bob-2"]
    assert rows[1]["first_name"] == "Hilda" and rows[0]["shirt"] == "M"
    rows[0]["github_id"], rows[0]["ip"] = "111", "203.0.113.9"
    ac.write_roster(str(path), rows)
    lines = path.read_text().splitlines()
    header = lines[0].split(",")
    assert header[:7] == [
        "notes",
        "github",
        "shirt",
        "first_name",
        "last_name",
        "repo",
        "github_id",
    ]
    assert set(header) == set(ac.ROSTER_COLUMNS) | {"shirt", "github_id"}
    again = ac.read_roster(str(path))
    assert [r["github"] for r in again] == ["alice", "", "bob-2"]
    assert again[1]["notes"] == "helper without a VM" and again[1]["shirt"] == "L"
    assert again[0]["ip"] == "203.0.113.9" and again[2]["shirt"] == "S"
    assert (tmp_path / "roster.csv.bak").read_text() == ROSTER_MESSY


def test_roster_write_back_preserves_the_file_mode(tmp_path):
    """A 0600 roster (participant data) must not come back 0644 from the temp file;
    a brand-new roster is created 0600; the `.bak` keeps the mode (codex round 2 on PR #46)."""
    import os
    import stat

    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    path.chmod(0o600)
    old_umask = os.umask(0o022)
    try:
        rows = ac.read_roster(str(path))
        ac.write_roster(str(path), rows)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "roster.csv.bak").stat().st_mode) == 0o600
        # an existing 0644 roster keeps 0644 (the owner's choice is respected)
        path.chmod(0o644)
        ac.write_roster(str(path), rows)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        # a new file, with no original to copy from, is restricted
        fresh = tmp_path / "new.csv"
        ac.write_roster(str(fresh), rows)
        assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
        # another writer's temp file (the old fixed name, or one in use right now) is neither
        # blocked on nor touched: each write has its own unique temp file (PM on #36, Mon 20:15)
        (tmp_path / "roster.csv.tmp").write_text("someone else's")
        ac.write_roster(str(path), rows)
        assert (tmp_path / "roster.csv.tmp").read_text() == "someone else's"
        assert not list(tmp_path.glob("roster.csv.*.tmp"))  # ours is gone after the replace
    finally:
        os.umask(old_umask)


def test_roster_ignores_the_github_column(tmp_path):
    """Issue #36 (Mon Oct 5): `provision_repos.py` writes `github` from Zulip and the launch
    ignores it, so neither a malformed value nor one shared by two rows blocks a launch."""
    path = tmp_path / "roster.csv"
    path.write_text(
        "slug,github,email\nada,alice,a@example.org\nbob,Alice,b@example.org\n"
        "cy,-bad login,c@example.org\n"
    )
    rows = ac.read_roster(str(path))
    assert [r["slug"] for r in ac.participants(rows)] == ["ada", "bob", "cy"]
    ac.validate_row({"slug": "ada", "github": "a/b ev il"})  # not checked here


def test_roster_rejects_bad_repo(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text("github,repo\nalice,bad repo name\n")
    with pytest.raises(ValueError):
        ac.read_roster(str(path))


def test_roster_reads_a_byte_order_mark_and_keeps_the_header(tmp_path):
    """PM review on #46: a BOM from a spreadsheet export made the first column `\\ufeffgithub`, so
    every participant vanished and a run with `--spares` still reported success."""
    path = tmp_path / "roster.csv"
    path.write_bytes(("\ufeff" + ROSTER).encode("utf-8"))
    rows = ac.read_roster(str(path))
    assert [r["github"] for r in rows] == ["alice", "bob-2"]
    assert ac.roster_fieldnames(str(path), rows)[0] == "github"
    ac.write_roster(str(path), rows)
    assert path.read_bytes()[:6] == b"github"  # written back without the mark
    assert [r["github"] for r in ac.read_roster(str(path))] == ["alice", "bob-2"]


@pytest.mark.parametrize(
    ("rows", "what"),
    [
        ("alice,a@example.org\nAlice,b@example.org\n", "slug"),
        ("alice,Al@Example.org\nbob,al@example.org\n", "email"),
    ],
)
def test_roster_refuses_duplicates_case_insensitively(tmp_path, rows, what, monkeypatch):
    """PM review on #46: the second of two rows with one login silently got the first row's VM."""
    path = tmp_path / "roster.csv"
    path.write_text("github,email\n" + rows)
    with pytest.raises(ValueError, match=f"duplicate {what}"):
        ac.read_roster(str(path))
    ec2 = FakeEc2()
    monkeypatch.setattr(launch, "Ec2", lambda region: ec2)
    assert (
        with_scope(
            launch.main, ["--roster", str(path), "--ami", "a", "--security-group-id", "sg-1"]
        )
        == 1
    )
    assert ec2.calls == []  # refused before any AWS call


def test_roster_header_names_are_normalised(tmp_path):
    """PM follow-up on #53: a `GitHub` header hid every participant just as the BOM did."""
    path = tmp_path / "roster.csv"
    path.write_text("GitHub,First_Name,Last_Name,Email,Repo\nalice,Alice,O'Neil,a@example.org,t\n")
    rows = ac.read_roster(str(path))
    assert rows[0]["github"] == "alice" and rows[0]["first_name"] == "Alice"
    assert ac.roster_fieldnames(str(path), rows)[:2] == ["github", "first_name"]
    ac.write_roster(str(path), rows)
    assert path.read_text().splitlines()[0].startswith("github,first_name,last_name,email,repo")
    assert ac.read_roster(str(path))[0]["email"] == "a@example.org"


# ---------------------------------------------------------------------------------- user data


def lines_of(script: str) -> list[str]:
    return [line for line in script.splitlines() if line and not line.startswith("#")]


def test_user_data_quotes_every_value_for_bash():
    row = {
        "slug": "bob-2",
        "first_name": "Bob",
        "last_name": "Smith; rm -rf /",
        "email": "bob+x@example.org",
        "repo": "o/sandbox",
    }
    script = ac.render_user_data(row)
    assert script.startswith("#!/bin/bash\n")
    assert "hostnamectl set-hostname ws-bob-2\n" in script
    assert "ssh-import-id" not in script
    assert "user.name 'Bob Smith; rm -rf /'\n" in script
    assert "user.email bob+x@example.org\n" in script
    participant = shlex.quote(json.dumps({"slug": "bob-2", "repo": "sandbox"}))
    assert f"echo {participant} > /etc/zab-participant.json\n" in script
    words = shlex.split(next(line for line in script.splitlines() if "user.name" in line))
    assert words[-1] == "Bob Smith; rm -rf /"
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)  # noqa: S607


def test_user_data_runs_every_step_without_set_e():
    """The first boot (workshop/ami/README.md): one failing step must not skip the identity,
    ~/work or the participant file."""
    script = ac.render_user_data(fresh_row())
    code = lines_of(script)
    assert not any(line.startswith("set -e") for line in code)
    assert code[0] == "set -uo pipefail"
    order = [
        next(i for i, line in enumerate(code) if "set-hostname" in line),
        next(i for i, line in enumerate(code) if "user.name" in line),
        next(i for i, line in enumerate(code) if "user.email" in line),
        next(i for i, line in enumerate(code) if "/home/agent/work" in line),
        next(i for i, line in enumerate(code) if "zab-participant.json" in line),
    ]
    assert order == sorted(order)
    assert "zab-import-key" not in script and "cron" not in script and "{" + "github}" not in script
    assert code[-1] == "exit 0"
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)  # noqa: S607


def test_first_boot_applies_swap_pins_and_mcp():
    """Drew's decisions relayed on issue #36 (Fri 02:45 and 03:10): an 8 GiB swap file, Claude
    Code's auto-updater off through managed settings, Codex's update check off, and the LMFDB MCP
    server for both CLIs, applied idempotently at first boot on participant and spare VMs."""
    for row in (fresh_row(), ac.spare_rows(1)[0]):
        script = ac.render_user_data(row)
        assert "fallocate -l 8G /swapfile" in script
        assert (
            "'/swapfile none swap sw,nofail 0 0'" in script
        )  # nofail: a lost swap file must not block boot
        assert "timeout 120 sudo -u agent" in script and "/var/log/zab-first-boot.log" in script
        assert "swapon --show=NAME --noheadings | grep -qx /swapfile" in script  # idempotent
        assert "/etc/claude-code/managed-settings.json" in script
        assert 'data.setdefault("env", {})["DISABLE_AUTOUPDATER"] = "1"' in script
        assert 'data["disableClaudeAiConnectors"] = True' in script
        assert "check_for_update_on_startup = false" in script
        assert "[mcp_servers.lmfdb]" in script and 'url = "https://mcp.lmfdb.org/mcp"' in script
        assert "mcp add --transport http --scope user lmfdb" in script
        assert "/home/agent/.local/bin/claude" in script  # the agent's install, run as agent
        assert script.count("https://mcp.lmfdb.org/mcp") == 2  # one per CLI
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)  # noqa: S607
    script = ac.render_user_data(fresh_row())
    code = lines_of(script)
    swap = next(i for i, line in enumerate(code) if "fallocate" in line)
    mcp = next(i for i, line in enumerate(code) if "mcp add" in line)
    participant = next(i for i, line in enumerate(code) if "zab-participant.json" in line)
    assert participant < swap < mcp
    # the image's README describes the same block ("First boot")
    doc = (ac_dir().parent / "ami" / "README.md").read_text()
    for needle in (
        "fallocate -l 8G /swapfile",
        "DISABLE_AUTOUPDATER",
        "check_for_update_on_startup = false",
        "[mcp_servers.lmfdb]",
        "mcp add --transport http --scope user lmfdb",
    ):
        assert needle in doc, needle


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("", "/swapfile none swap sw,nofail 0 0\n"),
        ("/swapfile none swap sw 0 0\n", None),  # an image's own line, by a space
        ("/swapfile\tnone\tswap\tsw\t0\t0\n", None),  # or by a tab (#799)
        ("/swapfile2 none swap sw 0 0\n", "/swapfile none swap sw,nofail 0 0\n"),  # another file
        ("#/swapfile none swap sw 0 0\n", "/swapfile none swap sw,nofail 0 0\n"),  # a comment
    ],
)
def test_first_boot_adds_the_swap_files_fstab_line_once(tmp_path, before, after):
    """The first boot's fstab line, run as the user data runs it, twice, against a copy of
    /etc/fstab: it adds its line only when no `/swapfile` line is there, whatever separates
    its fields, so that an image that carries one doesn't get a second."""
    line = next(x for x in lines_of(ac.render_user_data(fresh_row())) if "/etc/fstab" in x)
    fstab = tmp_path / "fstab"
    fstab.write_text(before)
    for _ in range(2):
        run = line.replace("/etc/fstab", shlex.quote(str(fstab)))
        subprocess.run(["bash", "-c", run], check=True)  # noqa: S603, S607
    assert fstab.read_text() == before + (after or "")


def ac_dir():
    import pathlib

    return pathlib.Path(ac.__file__).resolve().parent


def test_first_boot_codex_config_edits_keep_root_scope(tmp_path):
    """Codex round 1 on PR #53: appending the update key to a config that ends inside a table
    (`[projects."…"]`) would put it in that table. The block is run for real against such a file,
    with the agent paths redirected into tmp_path, and the result parsed as TOML."""
    import tomllib

    codexrc = tmp_path / "config.toml"
    codexrc.write_text(
        'approval_policy = "never"\n\n[projects."/home/agent/work"]\ntrust_level = "trusted"\n'
    )
    block = ac.COMMON_FIRST_BOOT
    start, end = block.index("codexrc="), block.index("claude_bin=")
    lines = block[start:end].replace("/home/agent/.codex/config.toml", str(codexrc))
    lines = lines.replace("-o agent -g agent ", "").replace("/home/agent/.codex", str(tmp_path))
    subprocess.run(["bash", "-euo", "pipefail", "-c", lines], check=True)  # noqa: S603, S607
    subprocess.run(["bash", "-euo", "pipefail", "-c", lines], check=True)  # noqa: S603, S607
    data = tomllib.loads(codexrc.read_text())
    assert data["check_for_update_on_startup"] is False  # root scope, not in the table
    assert data["mcp_servers"]["lmfdb"] == {"url": "https://mcp.lmfdb.org/mcp"}
    assert data["projects"]["/home/agent/work"] == {"trust_level": "trusted"}
    assert codexrc.read_text().count("check_for_update_on_startup") == 1


def test_spare_user_data_imports_nobody():
    rows = ac.spare_rows(2)
    assert [r["slug"] for r in rows] == ["zab-spare-1", "zab-spare-2"]
    assert all(ac.is_spare(r) for r in rows)
    script = ac.render_user_data(rows[0])
    assert "ssh-import-id" not in script and "cron" not in script
    assert "hostnamectl set-hostname ws-zab-spare-1\n" in script
    participant = shlex.quote(json.dumps({"slug": "", "repo": "", "spare": "zab-spare-1"}))
    assert f"echo {participant} > /etc/zab-participant.json\n" in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)  # noqa: S607
    # a roster row called spare-1 is a participant with its own namespace, not a spare
    assert not ac.is_spare({"slug": "spare-1"})
    assert "ws-spare-1\n" in ac.render_user_data({**fresh_row(), "slug": "spare-1"})
    # but the spare namespace itself is reserved: such a slug never reaches the tags
    with pytest.raises(ValueError, match="collides with the spare"):
        ac.validate_row({"slug": "zab-spare-1", "repo": ""})
    assert not ac.is_spare({"slug": "zab-spare-1"})  # without the marker it is not a spare


def test_a_login_named_like_a_spare_shares_no_resources():
    """Codex round 3 on PR #46: a participant `spare-1` plus `--spares 1` used to collapse into one
    VM (same `participant` tag); spares now live under `zab-spare-N`, which the roster refuses."""
    ec2 = FakeEc2()
    person = {**fresh_row(), "slug": "spare-1"}
    spares = ac.spare_rows(1)
    summary = launch.Summary()
    launch.launch(
        [person],
        ec2,
        opts(spares=1),
        log=quiet,
        ssh_ready=lambda ip: True,
        summary=summary,
        spares=spares,
    )
    runs = [c for c in ec2.calls if c[0] == "run_instance"]
    assert len(runs) == 2 and summary.launched == ["spare-1", "zab-spare-1"]
    tags = [ac.tags_of({"Tags": c[1]["TagSpecifications"][0]["Tags"]})["participant"] for c in runs]
    assert tags == ["spare-1", "zab-spare-1"]
    assert person["ip"] != spares[0]["ip"]
    assert all("ssh-import-id" not in run[1]["UserData"] for run in runs)


def test_main_refuses_a_roster_slug_in_the_spare_namespace(tmp_path, capsys, monkeypatch):
    path = tmp_path / "roster.csv"
    path.write_text("slug,repo\nzab-spare-1,thesis\n")
    ec2 = FakeEc2()
    monkeypatch.setattr(launch, "Ec2", lambda region: ec2)
    rc = with_scope(
        launch.main, ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-1"]
    )
    assert rc == 1 and "zab-spare-1" in capsys.readouterr().err
    assert ec2.calls == []  # refused before any AWS call, queries included


def test_user_data_quotes_apostrophes_and_needs_an_email():
    row = {
        "slug": "alice",
        "first_name": "Alice",
        "last_name": "O'Neil",
        "email": "alice@example.org",
        "repo": "thesis",
    }
    script = ac.render_user_data(row)
    words = shlex.split(next(line for line in script.splitlines() if "user.name" in line))
    assert words[-1] == "Alice O'Neil"
    with pytest.raises(ValueError, match="no e-mail"):
        ac.render_user_data({**row, "email": ""})
    with pytest.raises(ValueError):
        ac.render_user_data({**row, "slug": "bad slug"})


# ------------------------------------------------------------------------------ security group


def test_validate_security_group():
    ac.validate_security_group(GOOD_SG)
    with pytest.raises(ValueError, match="default"):
        ac.validate_security_group({"GroupId": "sg-d", "GroupName": "default", "IpPermissions": []})
    with pytest.raises(ValueError, match="no inbound rule"):
        ac.validate_security_group({"GroupId": "sg-e", "GroupName": "empty", "IpPermissions": []})
    wide = {**GOOD_SG, "IpPermissions": [{"IpProtocol": "-1"}]}
    with pytest.raises(ValueError, match="more than TCP 22"):
        ac.validate_security_group(wide)
    extra = {
        **GOOD_SG,
        "IpPermissions": GOOD_SG["IpPermissions"]
        + [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80}],
    }
    with pytest.raises(ValueError, match="80-80"):
        ac.validate_security_group(extra)


def test_main_requires_and_checks_the_security_group(tmp_path, capsys, monkeypatch):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    with pytest.raises(SystemExit):
        with_scope(launch.main, ["--roster", str(path), "--ami", "ami-1", "--dry-run"])
    assert "--security-group-id" in capsys.readouterr().err
    bad = FakeEc2()
    bad.security_groups["sg-default"] = {"GroupId": "sg-default", "GroupName": "default"}
    monkeypatch.setattr(launch, "Ec2", lambda region: bad)
    rc = with_scope(
        launch.main, ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-default"]
    )
    assert rc == 1 and "default security group" in capsys.readouterr().err
    assert not any(c[0] == "run_instance" for c in bad.calls)
    rc = with_scope(
        launch.main, ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-missing"]
    )
    assert rc == 1 and "not found" in capsys.readouterr().err


# ------------------------------------------------------------------------------------- launch


def test_launch_is_idempotent_and_fills_the_roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    rows = ac.read_roster(str(path))
    ec2 = FakeEc2()
    log: list[str] = []
    summary = launch.Summary()
    out = launch.launch(
        rows,
        ec2,
        opts(),
        log=log.append,
        ssh_ready=lambda ip: True,
        summary=summary,
    )
    assert [r["ip"] for r in out] == ["203.0.113.2", "203.0.113.4"]
    assert [r["host"] for r in out] == ["ws-alice", "ws-bob-2"]
    assert summary.launched == ["alice", "bob-2"] and not summary.failed
    run_calls = [c for c in ec2.calls if c[0] == "run_instance"]
    assert len(run_calls) == 2
    params = run_calls[0][1]
    assert params["ImageId"] == "ami-123" and params["InstanceType"] == "m7a.2xlarge"
    assert params["MetadataOptions"]["HttpTokens"] == "required"
    assert "IamInstanceProfile" not in params
    ebs = params["BlockDeviceMappings"][0]["Ebs"]
    assert (ebs["VolumeSize"], ebs["VolumeType"], ebs["DeleteOnTermination"]) == (200, "gp3", True)
    tags = ac.tags_of({"Tags": params["TagSpecifications"][0]["Tags"]})
    assert tags == {"Name": "ws-alice", "workshop": "nt-agents-2026", "participant": "alice"}
    assert params["KeyName"] == "drew" and params["SecurityGroupIds"] == ["sg-1"]
    assert "ssh-import-id" not in params["UserData"]
    assert "user.email alice@example.org\n" in params["UserData"]
    assert any(c[0] == "associate_address" for c in ec2.calls)
    # second run: both exist with their addresses attached; nothing launched or allocated
    ec2.calls.clear()
    summary = launch.Summary()
    launch.launch(
        out,
        ec2,
        opts(),
        log=log.append,
        ssh_ready=lambda ip: True,
        summary=summary,
    )
    kinds = [c[0] for c in ec2.calls]
    assert not any(k in ("run_instance", "allocate_address", "associate_address") for k in kinds)
    assert summary.existing == ["alice", "bob-2"]
    assert [r["ip"] for r in out] == ["203.0.113.2", "203.0.113.4"]


def test_an_edited_slug_names_the_previous_vm():
    """PM follow-up on #53: `host` names the VM of the slug as of the last run; a slug corrected
    in the roster afterwards gets a new VM, and the old one is named for teardown."""
    ec2 = FakeEc2()
    existing_instance(ec2, "alcie")  # the mistyped slug's VM, to be torn down separately
    row = {**fresh_row(), "host": "ws-alcie"}
    summary = launch.Summary()
    launch.launch([row], ec2, opts(), log=quiet, ssh_ready=lambda ip: True, summary=summary)
    assert row["host"] == "ws-alice" and summary.launched == ["alice"]
    assert ac.tags_of(ec2.instances["i-0002"])["participant"] == "alice"
    assert summary.old_vms == {"alice": "alcie"}
    assert "previous name 'alcie' may still have a VM (none after a dry run)" in summary.text()
    assert "teardown.py --only alcie --dry-run" in summary.text()


def test_errors_are_per_row_and_summarized():
    """A row without an e-mail (git's identity, issue #36) fails on its own; the others launch."""
    rows = [
        fresh_row(),
        {**fresh_row(), "slug": "bob-2", "email": ""},
        {**fresh_row(), "slug": "carol", "email": "carol@example.org"},
    ]
    ec2 = FakeEc2()
    log: list[str] = []
    summary = launch.Summary()
    launch.launch(rows, ec2, opts(), log=log.append, ssh_ready=lambda ip: True, summary=summary)
    assert summary.launched == ["alice", "carol"]
    assert list(summary.failed) == ["bob-2"] and "no e-mail" in summary.failed["bob-2"]
    assert rows[2]["ip"] and not rows[1]["ip"]
    assert sum(1 for c in ec2.calls if c[0] == "run_instance") == 2
    assert any(line.startswith("[bob-2] ERROR") for line in log)
    text = summary.text()
    assert "launched 2, existing 0, skipped 0, failed 1" in text and "FAILED bob-2" in text


def test_spares_launch_without_key_import_and_are_idempotent():
    ec2 = FakeEc2()
    spares = ac.spare_rows(2)
    summary = launch.Summary()
    launch.launch(
        [],
        ec2,
        opts(spares=2),
        log=quiet,
        ssh_ready=lambda ip: True,
        summary=summary,
        spares=spares,
    )
    runs = [c for c in ec2.calls if c[0] == "run_instance"]
    assert len(runs) == 2 and summary.launched == ["zab-spare-1", "zab-spare-2"]
    for call, name in zip(runs, ("zab-spare-1", "zab-spare-2"), strict=True):
        tags = ac.tags_of({"Tags": call[1]["TagSpecifications"][0]["Tags"]})
        assert tags == {"Name": f"ws-{name}", "workshop": "nt-agents-2026", "participant": name}
        assert "ssh-import-id" not in call[1]["UserData"]
    assert all(s["ip"] for s in spares)
    assert "spare" in ac.ssh_config_snippet(spares[0])
    ec2.calls.clear()
    launch.launch(
        [],
        ec2,
        opts(spares=2),
        log=quiet,
        ssh_ready=lambda ip: True,
        spares=ac.spare_rows(2),
    )
    assert not any(c[0] == "run_instance" for c in ec2.calls)


def existing_instance(ec2: FakeEc2, name: str) -> str:
    tags = ac.tag_list(launch.participant_tags(name, FLEET_SCOPE))
    iid = ec2.run_instance(TagSpecifications=[{"ResourceType": "instance", "Tags": tags}])
    ec2.calls.clear()
    return iid


def test_rerun_reconciles_a_missing_or_detached_elastic_ip():
    """A run that died between run_instances and the address steps is completed on rerun."""
    kw = {"log": quiet, "ssh_ready": lambda ip: True}
    # (a) instance exists, no address at all: allocate and associate, no new instance
    ec2 = FakeEc2()
    iid = existing_instance(ec2, "alice")
    out = launch.launch([fresh_row()], ec2, opts(), **kw)
    kinds = [c[0] for c in ec2.calls]
    assert "run_instance" not in kinds
    assert kinds.count("allocate_address") == 1 and kinds.count("associate_address") == 1
    assert out[0]["ip"] == "203.0.113.2" and out[0]["host"] == "ws-alice"
    assert next(iter(ec2.addresses.values()))["InstanceId"] == iid
    # (b) address allocated and tagged but never associated: associate only
    ec2 = FakeEc2()
    iid = existing_instance(ec2, "alice")
    aid = ec2.allocate_address(ac.tag_list(launch.participant_tags("alice", FLEET_SCOPE)))
    ec2.calls.clear()
    out = launch.launch([fresh_row()], ec2, opts(), **kw)
    kinds = [c[0] for c in ec2.calls]
    assert "allocate_address" not in kinds and ("associate_address", aid, iid) in ec2.calls
    assert out[0]["ip"] == ec2.addresses[aid]["PublicIp"]
    # (c) dry run of (a): queries only, ip left empty
    ec2 = FakeEc2()
    existing_instance(ec2, "alice")
    log: list[str] = []
    out = launch.launch([fresh_row()], ec2, opts(dry_run=True), **{**kw, "log": log.append})
    assert all(c[0] in ("find_instances", "find_addresses") for c in ec2.calls)
    assert out[0]["ip"] == "" and any("would allocate and associate" in line for line in log)


def test_launch_only_and_dry_run(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    rows = ac.read_roster(str(path))
    ec2 = FakeEc2()
    log: list[str] = []
    launch.launch(
        rows,
        ec2,
        opts(only="bob-2", dry_run=True),
        log=log.append,
        ssh_ready=lambda ip: True,
    )
    assert all(c[0] in ("find_instances", "find_addresses") for c in ec2.calls)
    assert any("would run_instances" in line and "ws-bob-2" in line for line in log)
    assert not any("alice" in line for line in log)


def test_main_dry_run_needs_no_boto3_and_no_network(tmp_path, capsys, monkeypatch):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)

    def no_network(*a, **k):
        raise AssertionError("the offline test must not reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setitem(sys.modules, "boto3", None)  # importing boto3 raises ImportError
    rc = with_scope(
        launch.main,
        [
            "--roster",
            str(path),
            "--ami",
            "ami-1",
            "--security-group-id",
            "sg-1",
            "--dry-run",
            "--spares",
            "1",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "GitHub" not in out
    assert "would run_instances" in out and "ws-zab-spare-1" in out
    assert "not checked" in out  # the security group cannot be inspected without boto3
    again = ac.read_roster(str(path))
    assert again[0]["github_id"] == "" and again[1]["github_id"] == "4242"  # left as they were
    # a dry run records nothing, so the roster is not even rewritten (PM, Mon 22:30)
    assert path.read_text() == ROSTER and not (tmp_path / "roster.csv.bak").exists()
    assert "launched 3, existing 0" in out


def test_main_writes_the_roster_even_when_launch_blows_up(tmp_path, monkeypatch, capsys):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    ec2 = FakeEc2()
    monkeypatch.setattr(launch, "Ec2", lambda region: ec2)

    def boom(rows, *a, **k):
        rows[0]["ip"] = "203.0.113.77"
        raise RuntimeError("AWS exploded")

    monkeypatch.setattr(launch, "launch", boom)
    with pytest.raises(RuntimeError):
        with_scope(
            launch.main, ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-1"]
        )
    assert ac.read_roster(str(path))[0]["ip"] == "203.0.113.77"


def test_main_exit_code_reflects_failures(tmp_path, monkeypatch, capsys):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER.replace("alice@example.org", ""))  # alice: no e-mail, fails
    ec2 = FakeEc2()
    monkeypatch.setattr(launch, "Ec2", lambda region: ec2)
    monkeypatch.setattr(launch, "wait_for_ssh", lambda ip: True)
    rc = with_scope(
        launch.main, ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-1"]
    )
    out = capsys.readouterr().out
    assert rc == 1 and "FAILED alice" in out and "Host ws-bob-2" in out
    assert ac.read_roster(str(path))[1]["ip"].startswith("203.0.113.")


def test_lookups_ignore_resources_of_another_workshop():
    """`participant=alice` from another event (or an untagged ws-alice) must not be adopted,
    nor its spare Elastic IP attached (codex round 3 on PR #19)."""
    ec2 = FakeEc2()
    other = ac.tag_list({"Name": "ws-alice", "workshop": "another-event", "participant": "alice"})
    untagged = ac.tag_list({"Name": "ws-alice"})
    ec2.run_instance(TagSpecifications=[{"ResourceType": "instance", "Tags": other}])
    ec2.run_instance(TagSpecifications=[{"ResourceType": "instance", "Tags": untagged}])
    spare = ec2.allocate_address(other)  # unassociated, tagged for the other event
    ec2.calls.clear()
    out = launch.launch([fresh_row()], ec2, opts(), log=quiet, ssh_ready=lambda ip: True)
    kinds = [c[0] for c in ec2.calls]
    assert kinds.count("run_instance") == 1, "a new instance for THIS workshop"
    assert kinds.count("allocate_address") == 1, "the other event's address is not attached"
    assert ec2.addresses[spare].get("InstanceId") is None
    assert out[0]["ip"] != ec2.addresses[spare]["PublicIp"]
    new = [i for i in ec2.instances.values() if ac.tags_of(i).get("workshop") == "nt-agents-2026"]
    assert len(new) == 1 and ac.tags_of(new[0])["participant"] == "alice"
    for call in ec2.calls:
        if call[0] in ("find_instances", "find_addresses"):
            assert call[1] == {"workshop": "nt-agents-2026", "participant": "alice"}


def test_tag_override_scopes_lookups_and_creation():
    ec2 = FakeEc2()
    existing = ac.tag_list(
        {"Name": "ws-alice", "workshop": "nt-agents-2026", "participant": "alice"}
    )
    ec2.run_instance(TagSpecifications=[{"ResourceType": "instance", "Tags": existing}])
    ec2.calls.clear()
    launch.launch(
        [fresh_row()],
        ec2,
        opts(tag="workshop=dry-run-2026"),
        log=quiet,
        ssh_ready=lambda ip: True,
    )
    run_calls = [c for c in ec2.calls if c[0] == "run_instance"]
    assert len(run_calls) == 1, "the main workshop's instance is out of scope"
    tags = ac.tags_of({"Tags": run_calls[0][1]["TagSpecifications"][0]["Tags"]})
    assert tags["workshop"] == "dry-run-2026"
    assert ec2.calls[0] == (
        "find_instances",
        {"workshop": "dry-run-2026", "participant": "alice"},
        launch.LIVE_STATES,
    )


#: Each script's arguments, complete but for the scope and the region
COMPLETE = {
    launch.main: ["--roster", "r.csv", "--ami", "ami-1", "--security-group-id", "sg-1"],
    teardown.main: [],
    protect.main: [],
}


@pytest.mark.parametrize(("missing", "kept"), [("--tag", "--region"), ("--region", "--tag")])
def test_the_scripts_share_one_scope_and_require_it_and_the_region(missing, kept, capsys):
    """#808: no default scope or region, which would name a deployment, and reach its VMs from
    a script run without them: each script stops at argparse, naming the one missing."""
    assert launch.parse_tags is teardown.parse_tags is protect.parse_tags is ac.parse_tags
    for specs in ("workshop=nt-agents-2026", ["workshop=nt-agents-2026"]):
        assert ac.parse_tags(specs) == {"workshop": "nt-agents-2026"}
    for specs in (None, []):
        with pytest.raises(ValueError, match="no scope: give the workshop's --tag"):
            ac.parse_tags(specs)
    given = {"--tag": "workshop=w", "--region": "us-east-2"}
    for main, argv in COMPLETE.items():
        with pytest.raises(SystemExit) as stop:
            main([*argv, kept, given[kept], "--dry-run"])
        assert stop.value.code == 2
        assert f"the following arguments are required: {missing}\n" in capsys.readouterr().err


# ------------------------------------------------- several scope tags (the test rig, issue #263)

RIG_TAGS = ["zab-role=test", "workshop=zab-test"]
RIG_SCOPE = {"zab-role": "test", "workshop": "zab-test"}


def test_parse_tags_repeatable_and_refusals():
    assert ac.parse_tags(RIG_TAGS) == RIG_SCOPE
    assert ac.parse_tags([*RIG_TAGS, "workshop=zab-test"]) == RIG_SCOPE  # same value twice: fine
    for bad in (
        ["workshop=a", "workshop=b"],  # one key, two values
        ["participant=alice"],  # set per participant
        ["Name=ws-x"],
        ["zab-role"],
    ):
        with pytest.raises(ValueError):
            ac.parse_tags(bad)
    with pytest.raises(ValueError):
        ac.scope_tags({})  # an empty scope would match everything
    assert ac.scope_tags(("workshop", "x")) == {"workshop": "x"}  # the old single-tag form
    assert ac.scope_text(RIG_SCOPE) == "zab-role=test, workshop=zab-test"
    assert ac.scope_label(("workshop", "nt-agents-2026")) == "nt-agents-2026"  # spares.json
    assert ac.scope_label(RIG_SCOPE) == "workshop=zab-test,zab-role=test"


def test_launch_with_two_scope_tags_tags_everything_and_looks_up_by_both():
    """The test VM (#263 setup step 3): every resource carries both tags, no termination
    protection, the fleet's type; a VM carrying only one of the tags is not adopted."""
    ec2 = FakeEc2()
    half = ac.tag_list({"Name": "ws-alice", "workshop": "zab-test", "participant": "alice"})
    ec2.run_instance(TagSpecifications=[{"ResourceType": "instance", "Tags": half}])
    ec2.calls.clear()
    launch.launch([fresh_row()], ec2, opts(tag=RIG_TAGS), log=quiet, ssh_ready=lambda ip: True)
    assert ec2.calls[0] == (
        "find_instances",
        {**RIG_SCOPE, "participant": "alice"},
        launch.LIVE_STATES,
    )
    (run,) = [c[1] for c in ec2.calls if c[0] == "run_instance"]
    want = {"Name": "ws-alice", **RIG_SCOPE, "participant": "alice"}
    for spec in run["TagSpecifications"]:
        assert ac.tags_of(spec) == want, spec["ResourceType"]
    assert "DisableApiTermination" not in run and run["InstanceType"] == "m7a.2xlarge"
    (alloc,) = [c[1] for c in ec2.calls if c[0] == "allocate_address"]
    assert ac.tags_of({"Tags": alloc}) == want
    # teardown and protect see the new VM only, never the half-tagged one
    instances, addresses, _ = teardown.plan(ec2, RIG_SCOPE, None)
    assert [i["InstanceId"] for i in instances] == ["i-0002"] and len(addresses) == 1
    log: list[str] = []
    assert protect.protect(ec2, RIG_SCOPE, dry_run=True, log=log.append) == 0
    assert [line.split()[1] for line in log[:-1]] == ["i-0002"]
    assert protect.protect(ec2, RIG_SCOPE, only=["bob"], log=log.append) == 1
    assert log[-1] == "bob: no live instance with zab-role=test, workshop=zab-test"


def test_scripts_parse_repeated_tags(monkeypatch, tmp_path):
    seen = {}

    def fake_teardown(ec2, tag, **kw):
        seen["teardown"] = tag
        return 0

    def fake_protect(ec2, tag, **kw):
        seen["protect"] = tag
        return 0

    monkeypatch.setattr(teardown, "Ec2", lambda region: object())
    monkeypatch.setattr(teardown, "teardown", fake_teardown)
    monkeypatch.setattr(protect, "Ec2", lambda region: object())
    monkeypatch.setattr(protect, "protect", fake_protect)
    args = ["--tag", RIG_TAGS[0], "--tag", RIG_TAGS[1]]
    assert with_scope(teardown.main, args) == 0 and with_scope(protect.main, args) == 0
    assert seen == {"teardown": RIG_SCOPE, "protect": RIG_SCOPE}
    assert with_scope(teardown.main, ["--tag", "participant=x"]) == 1


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kw):
        return self.pages


class _Boto:
    """What boto3 answers, unfiltered: the real `Ec2` must still keep only fully tagged ones."""

    def __init__(self):
        both = [{"Key": "zab-role", "Value": "test"}, {"Key": "workshop", "Value": "zab-test"}]
        self.instances = [
            {"InstanceId": "i-both", "Tags": both},
            {"InstanceId": "i-one", "Tags": both[1:]},
        ]
        self.filters = []

    def get_paginator(self, name):
        return _Paginator([{"Reservations": [{"Instances": self.instances}]}])

    def describe_addresses(self, Filters):
        self.filters.append(Filters)
        return {"Addresses": [{"AllocationId": "a", "Tags": self.instances[1]["Tags"]}]}


def test_real_ec2_requires_every_tag():
    ec2 = object.__new__(ac.Ec2)
    ec2._ec2 = _Boto()
    found = ec2.find_instances(RIG_SCOPE, ("running",))
    assert [i["InstanceId"] for i in found] == ["i-both"]
    assert ec2.find_addresses(RIG_SCOPE) == []
    assert ec2._ec2.filters[0] == [
        {"Name": "tag:zab-role", "Values": ["test"]},
        {"Name": "tag:workshop", "Values": ["zab-test"]},
    ]
    for call in (lambda: ec2.find_instances({}, ("running",)), lambda: ec2.find_addresses({})):
        with pytest.raises(ValueError):
            call()


def test_ssh_from_restricts_the_security_group_sources():
    rule = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22}
    one = {**GOOD_SG, "IpPermissions": [{**rule, "IpRanges": [{"CidrIp": "198.51.100.7/32"}]}]}
    ac.validate_security_group(one, ["198.51.100.7"])  # a bare address is its /32
    ac.validate_security_group(GOOD_SG)  # without --ssh-from, any source as before
    with pytest.raises(ValueError, match=r"0\.0\.0\.0/0"):
        ac.validate_security_group(GOOD_SG, ["198.51.100.7"])
    pairs = [{**one["IpPermissions"][0], "UserIdGroupPairs": [{"GroupId": "sg-9"}]}]
    other_group = {**one, "IpPermissions": pairs}
    with pytest.raises(ValueError, match="sg-9"):
        ac.validate_security_group(other_group, ["198.51.100.7/32"])
    with pytest.raises(ValueError):
        ac.parse_cidr("devbox")
    ec2 = FakeEc2()
    ec2.security_groups["sg-1"] = one
    log: list[str] = []
    launch.check_security_group(ec2, "sg-1", log.append, ssh_from=["198.51.100.7/32"])
    assert "from 198.51.100.7/32 only" in log[0]


def test_one_row_test_roster_launches_by_slug(tmp_path):
    """The rig's roster (`~/.config/zab/test-roster.csv`): one row, slug `zab-test`."""
    path = tmp_path / "test-roster.csv"
    path.write_text("slug,first_name,last_name,email\nzab-test,Zab,Tester,zab-tester@example.org\n")
    rows = ac.read_roster(str(path))
    ec2 = FakeEc2()
    launch.launch(
        rows, ec2, opts(tag=RIG_TAGS, only="zab-test"), log=quiet, ssh_ready=lambda ip: True
    )
    assert rows[0]["host"] == "ws-zab-test" and rows[0]["ip"].startswith("203.0.113.")
    (run,) = [c[1] for c in ec2.calls if c[0] == "run_instance"]
    assert ac.tags_of(run["TagSpecifications"][0])["participant"] == "zab-test"


def test_ssh_snippet():
    """Organizers only, as `ubuntu`: participants get no SSH (issue #36, Mon Oct 5)."""
    for row in (
        {"slug": "alice", "ip": "1.2.3.4"},
        {"slug": "alice", "github": "alice", "ip": "1.2.3.4"},
    ):
        assert (
            ac.ssh_config_snippet(row) == "Host ws-alice\n    HostName 1.2.3.4\n    User ubuntu\n"
        )


def test_wait_for_ssh_times_out_and_succeeds():
    clock = iter(range(0, 1000, 5))
    attempts = []
    slept: list[float] = []

    def connect(addr, timeout):
        attempts.append(addr)
        raise OSError("refused")

    assert (
        ac.wait_for_ssh(
            "1.2.3.4",
            timeout_s=20,
            connect=connect,
            sleep=slept.append,
            clock=lambda: next(clock),
        )
        is False
    )
    assert attempts and attempts[0] == ("1.2.3.4", 22)
    assert slept == [5.0] * len(attempts)  # 5 s between tries, never a busy loop

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    assert (
        ac.wait_for_ssh(
            "1.2.3.4",
            timeout_s=20,
            connect=lambda a, t: Conn(),
            sleep=lambda s: None,
            clock=lambda: 0,
        )
        is True
    )


# ----------------------------------------------------------------------------------- teardown


def populated() -> FakeEc2:
    ec2 = FakeEc2()
    for name in ("alice", "bob-2"):
        tags = ac.tag_list(
            {"Name": f"ws-{name}", "workshop": "nt-agents-2026", "participant": name}
        )
        iid = ec2.run_instance(TagSpecifications=[{"ResourceType": "instance", "Tags": tags}])
        aid = ec2.allocate_address(tags)
        ec2.associate_address(aid, iid)
    ec2.calls.clear()
    return ec2


def test_teardown_asks_then_terminates_and_releases():
    ec2 = populated()
    log: list[str] = []
    rc = teardown.teardown(
        ec2,
        ("workshop", "nt-agents-2026"),
        region="us-west-2",
        confirm=lambda q: "y",
        log=log.append,
    )
    assert rc == 0
    kinds = [c[0] for c in ec2.calls]
    assert kinds.count("terminate") == 1 and kinds.count("wait_terminated") == 1
    assert kinds.count("release_address") == 2
    assert all(i["State"]["Name"] == "shutting-down" for i in ec2.instances.values())
    assert not ec2.addresses


def test_teardown_declined_or_dry_run_changes_nothing():
    for kwargs in ({"confirm": lambda q: "n"}, {"dry_run": True}):
        ec2 = populated()
        log: list[str] = []
        rc = teardown.teardown(
            ec2, ("workshop", "nt-agents-2026"), region="us-west-2", log=log.append, **kwargs
        )
        assert rc == (2 if "confirm" in kwargs else 0)
        assert not any(c[0] in ("terminate", "release_address") for c in ec2.calls)
        assert len(ec2.addresses) == 2
        assert any("terminate i-0001 ws-alice" in line for line in log)


def test_teardown_only_one_participant():
    ec2 = populated()
    rc = teardown.teardown(
        ec2, ("workshop", "nt-agents-2026"), region="us-west-2", only="bob-2", yes=True, log=quiet
    )
    assert rc == 0
    assert [c for c in ec2.calls if c[0] == "terminate"] == [("terminate", ["i-0003"])]
    assert list(ec2.addresses) == ["eipalloc-0002"]


def test_teardown_nothing_matching():
    ec2 = FakeEc2()
    assert (
        teardown.teardown(ec2, ("workshop", "other"), region="us-west-2", yes=True, log=quiet) == 0
    )


@pytest.mark.parametrize("bad", ["workshop", "=x", "k="])
def test_parse_tag_rejects(bad):
    with pytest.raises(ValueError):
        teardown.parse_tags([bad])


def test_modules_import_without_boto3(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(ImportError):
        ac.Ec2("us-west-2")
    assert launch.instance_params and teardown.plan and protect.protect


def test_the_fleet_default_is_m7a_2xlarge():
    """D23 (Drew, Sat Oct 3): 8 vCPUs and 32 GiB for Mathlib, superseding D14's m7a.xlarge."""
    assert launch.INSTANCE_TYPE == "m7a.2xlarge"
    assert "[--instance-type m7a.2xlarge]" in launch.__doc__


def test_first_boot_warms_the_volume_in_the_background(tmp_path):
    """A fresh v2 VM took over 15 minutes for its first `import Mathlib` (EBS lazy loading, PR
    #79): every first boot reads the big trees once, Lean first, in a transient systemd unit."""
    assert ac.WARM_PATHS[:3] == ("/home/agent/.elan", "/opt/lean", "/home/agent/lean")
    block = ac.COMMON_FIRST_BOOT
    assert "systemd-run --unit=zab-warm-volume --nice=19" in block
    assert "IOSchedulingPriority=7" in block and "touch /var/lib/zab-warm.done" in block
    for row in (
        {"slug": "alice", "first_name": "A", "last_name": "B", "email": "a@example.org"},
        ac.spare_rows(1)[0],
    ):
        assert "zab-warm-volume" in ac.render_user_data(row)
    # the read itself, over a scratch tree: every file is read, a missing directory is skipped
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f").write_text("x")
    cmd = ac.WARM_VOLUME.replace(" ".join(ac.WARM_PATHS), f"{tmp_path}/a {tmp_path}/missing")
    subprocess.run(["bash", "-c", "set -o pipefail; " + cmd], check=True)  # noqa: S603, S607


def test_first_boot_warms_the_shared_mathlib_behind_the_workspace_link(tmp_path):
    """Review on #259: with the shared Mathlib (issue #205), ~/lean/workspace/.lake/packages is
    a link to /opt/lean/packages/<tag>, and `find` doesn't follow it."""
    olean = tmp_path / "opt/lean/packages/v4.34.1/mathlib/.lake/build/lib/lean/Mathlib.olean"
    olean.parent.mkdir(parents=True)
    olean.write_bytes(b"olean")
    lake = tmp_path / "home/agent/lean/workspace/.lake"
    lake.mkdir(parents=True)
    (lake / "packages").symlink_to(tmp_path / "opt/lean/packages/v4.34.1")
    read = tmp_path / "read"
    cmd = ac.WARM_VOLUME.replace(
        " ".join(ac.WARM_PATHS), " ".join(f"{tmp_path}{p}" for p in ac.WARM_PATHS)
    ).replace("cat > /dev/null 2>&1", f"ls > {read}")
    subprocess.run(["bash", "-c", "set -o pipefail; " + cmd], check=True)  # noqa: S603, S607
    assert read.read_text().split() == [str(olean)]  # read once, through /opt/lean


# ------------------------------------------------------------ no GitHub (issue #36, Sun Oct 4)


def test_a_roster_without_github_launches_by_slug(tmp_path):
    """Drew's onboarding plan: participants need no GitHub account; the slug names everything,
    no GitHub request is made, and the first boot imports no keys."""
    path = tmp_path / "roster.csv"
    path.write_text(
        "slug,first_name,last_name,email\nada-l,Ada,Lovelace,ada@example.org\n"
        "kurt,Kurt,G,kurt@example.org\n"
    )
    rows = ac.read_roster(str(path))
    assert [r["slug"] for r in ac.participants(rows)] == ["ada-l", "kurt"]
    ec2 = FakeEc2()
    summary = launch.Summary()
    launch.launch(
        rows,
        ec2,
        opts(),
        log=quiet,
        ssh_ready=lambda ip: True,
        summary=summary,
    )
    assert summary.launched == ["ada-l", "kurt"] and not summary.failed
    tags = ac.tags_of(ec2.instances["i-0001"])
    assert tags["participant"] == "ada-l" and tags["Name"] == "ws-ada-l"
    assert rows[0]["host"] == "ws-ada-l"
    script = ac.render_user_data(rows[0])
    assert "hostnamectl set-hostname ws-ada-l\n" in script
    assert "ssh-import-id" not in script and "cron" not in script
    assert "user.email ada@example.org\n" in script and "user.name 'Ada Lovelace'\n" in script
    record = json.loads(
        shlex.split(next(ln for ln in script.splitlines() if "participant.json" in ln))[1]
    )
    assert record == {"slug": "ada-l", "repo": "ada-l"}
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)  # noqa: S607
    assert "User ubuntu" in ac.ssh_config_snippet({**rows[0], "ip": "203.0.113.1"})


@pytest.mark.parametrize(
    "bad",
    ["Ada", "-ada", "ada-", "a_b", "a" * 33, "zab-spare-3", ""],
)
def test_slugs_are_restricted(bad):
    if bad == "":
        with pytest.raises(ValueError, match="invalid slug"):
            ac.validate_row({"slug": "", "github": ""})
        return
    with pytest.raises(ValueError):
        ac.validate_row({"slug": bad, "github": ""})


def test_email_is_checked_and_needed():
    with pytest.raises(ValueError, match="invalid e-mail"):
        ac.validate_row({"slug": "ada", "email": "ada at example.org"})
    with pytest.raises(ValueError, match="no e-mail"):
        ac.render_user_data({"slug": "ada", "github": "", "first_name": "Ada"})
    # a login and its id no longer stand in for the e-mail (no no-reply address; issue #36)
    with pytest.raises(ValueError, match="no e-mail"):
        ac.render_user_data({"slug": "ada", "github": "ada", "github_id": "5", "first_name": "A"})


def test_duplicate_slugs_and_emails_are_refused(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text("slug,email\nada,a@example.org\nada,b@example.org\n")
    with pytest.raises(ValueError, match="duplicate slug"):
        ac.read_roster(str(path))
    path.write_text("slug,email\nada,a@example.org\nbob,A@example.org\n")
    with pytest.raises(ValueError, match="duplicate email"):
        ac.read_roster(str(path))


def test_github_columns_play_no_part_in_the_launch(monkeypatch):
    """Issue #36 (Mon Oct 5): no participant SSH. A row with `github`, `github_id` and
    `github_id_login` (written by provision_repos.py, or left from an older roster) launches
    exactly like one without: no GitHub request, no key import, the roster e-mail for git, and
    no login in the owner record."""

    def no_network(*a, **k):
        raise AssertionError("no GitHub request may be made")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    plain = {**fresh_row(), "first_name": "Alice", "last_name": "O'Neil"}
    with_login = {**plain, "github": "alice", "github_id": "1", "github_id_login": "alicia"}
    ec2 = FakeEc2()
    summary = launch.Summary()
    launch.launch(
        [dict(with_login)], ec2, opts(), log=quiet, ssh_ready=lambda ip: True, summary=summary
    )
    assert summary.launched == ["alice"] and not summary.failed
    script = next(c[1]["UserData"] for c in ec2.calls if c[0] == "run_instance")
    assert script == ac.render_user_data(plain)
    for needle in ("ssh-import-id", "zab-import-key", "/etc/cron.d", "noreply"):
        assert needle not in script, needle
    assert "user.email alice@example.org\n" in script
    record = json.loads(
        shlex.split(next(ln for ln in script.splitlines() if "participant.json" in ln))[1]
    )
    assert record == {"slug": "alice", "repo": "thesis"} and "github" not in record
    # a rerun with an edited login reuses the VM: no login check, nothing refused
    summary = launch.Summary()
    launch.launch(
        [{**with_login, "github": "alice-new", "github_id": ""}],
        ec2,
        opts(),
        log=quiet,
        ssh_ready=lambda ip: True,
        summary=summary,
    )
    assert summary.existing == ["alice"] and not summary.failed
    assert "User ubuntu" in ac.ssh_config_snippet({**with_login, "ip": "203.0.113.1"})


@pytest.mark.parametrize(
    "flag", [["--verify-logins"], ["--allow-name-mismatch", "alice"]], ids=["verify", "mismatch"]
)
def test_main_has_no_github_options(tmp_path, capsys, flag):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    with pytest.raises(SystemExit) as exc:
        with_scope(
            launch.main, ["--roster", str(path), "--ami", "a", "--security-group-id", "sg-1", *flag]
        )
    assert exc.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


def test_a_legacy_mixed_case_vm_is_reused_not_duplicated(tmp_path):
    """Codex round 1 on #97: `participant=AndrewVSutherland` was launched from a login-only
    roster; the derived slug is lower-case and EC2 tags are case-sensitive."""
    path = tmp_path / "roster.csv"
    path.write_text("github,first_name,last_name,email\nAliceW,Alice,W,a@example.org\n")
    rows = ac.read_roster(str(path))
    assert rows[0]["slug"] == "alicew"
    ec2 = FakeEc2()
    existing_instance(ec2, "AliceW")
    summary = launch.Summary()
    launch.launch(rows, ec2, opts(), log=quiet, ssh_ready=lambda ip: True, summary=summary)
    assert summary.launched == [] and summary.existing == ["alicew"]
    assert summary.legacy == {"alicew": "AliceW"} and "participant=AliceW" in summary.text()
    assert rows[0]["host"] == "ws-AliceW"
    ac.write_roster(str(path), rows)
    assert "slug_given" not in path.read_text()  # internal key, never a column
    # codex round 2 on #97: after the save the slug is a column, and the recorded host still
    # leads back to the legacy tag, so the second run reuses it too
    again = ac.read_roster(str(path))
    assert again[0]["slug"] == "alicew" and again[0].get("slug_given") == "1"
    summary = launch.Summary()
    launch.launch(again, ec2, opts(), log=quiet, ssh_ready=lambda ip: True, summary=summary)
    assert summary.launched == [] and summary.legacy == {"alicew": "AliceW"}
    assert not summary.old_vms  # the same VM, not a previous one
    # an explicit slug is the participant's own: no legacy lookup
    explicit = [{**rows[0], "slug": "alicew", "slug_given": "1", "host": ""}]
    summary = launch.Summary()
    launch.launch(explicit, ec2, opts(), log=quiet, ssh_ready=lambda ip: True, summary=summary)
    assert summary.launched == ["alicew"] and not summary.failed


# ----------------------------------------------------------------- regions (PM on #36, Sun 13:45)


def nameless_row(**kw):
    """A slug row without a GitHub login."""
    return {"slug": "alice", "github": "", "email": "a@example.org", "repo": "", "ip": "",
            "host": "", "region": "", **kw}  # fmt: skip


def test_a_row_recorded_in_another_region_is_left_alone():
    """launch.py sees instances only in --region: a second region must not duplicate a VM."""
    ec2 = FakeEc2()
    row = nameless_row(host="ws-alice", ip="198.51.100.4", region="us-east-2")
    summary = launch.Summary()
    launch.launch([row], ec2, opts(region="us-west-2"), log=quiet, summary=summary)
    assert ec2.calls == [] and not summary.failed and not summary.launched
    assert "us-east-2" in summary.other_region["alice"] and row["region"] == "us-east-2"
    assert "other region alice" in summary.text()


def test_the_region_is_recorded_for_new_and_existing_vms():
    ec2 = FakeEc2()
    new = nameless_row()
    launch.launch([new], ec2, opts(region="us-west-2"), log=quiet, ssh_ready=lambda ip: True)
    assert new["region"] == "us-west-2" and new["host"] == "ws-alice"
    ec2 = FakeEc2()
    existing_instance(ec2, "alice")
    old = nameless_row(host="ws-alice")  # launched before the column: found here, recorded
    summary = launch.Summary()
    launch.launch([old], ec2, opts(region="us-west-2"), log=quiet, summary=summary)
    assert summary.existing == ["alice"] and old["region"] == "us-west-2"


def test_a_vm_from_before_the_region_column_that_is_not_here_is_left_alone():
    """Today's rc3/rc4 VMs have `host` but no `region`: in another region they look missing."""
    ec2 = FakeEc2()
    row = nameless_row(host="ws-alice", ip="198.51.100.4")
    summary = launch.Summary()
    launch.launch([row], ec2, opts(region="us-east-2"), log=quiet, summary=summary)
    assert not any(c[0] == "run_instance" for c in ec2.calls) and not summary.failed
    assert "has no VM in us-east-2" in summary.other_region["alice"] and row["region"] == ""


def test_an_edited_region_cannot_duplicate_a_vm():
    """codex round 2 on #103: a row launched in us-west-2, then re-marked us-east-2 by hand,
    has host ws-<slug> but no VM in us-east-2: left alone, not launched again."""
    ec2 = FakeEc2()  # the us-east-2 view: the VM is in us-west-2
    row = nameless_row(host="ws-alice", ip="198.51.100.4", region="us-east-2")
    summary = launch.Summary()
    launch.launch([row], ec2, opts(region="us-east-2"), log=quiet, summary=summary)
    assert not any(c[0] == "run_instance" for c in ec2.calls)
    assert "clear host and ip" in summary.other_region["alice"]
    # cleared host and ip (the VM is really gone): a new VM is launched
    row.update(host="", ip="")
    launch.launch([row], ec2, opts(region="us-east-2"), log=quiet, ssh_ready=lambda ip: True)
    assert sum(1 for c in ec2.calls if c[0] == "run_instance") == 1


def test_a_dry_run_records_no_region():
    ec2 = FakeEc2()
    row = nameless_row()
    launch.launch([row], ec2, opts(region="us-west-2", dry_run=True), log=quiet)
    assert row["region"] == "" and not any(c[0] == "run_instance" for c in ec2.calls)


def test_the_roster_has_a_region_column():
    assert "region" in ac.ROSTER_COLUMNS


def test_a_dry_run_then_a_real_run_launches(tmp_path, monkeypatch):
    """codex round 1 on #103: the dry run recorded host ws-<slug>, which the next real run took
    for a VM launched in another region, and launched nothing."""
    path = tmp_path / "roster.csv"
    path.write_text("slug,first_name,last_name,email\nalice,Alice,Smith,a@example.org\n")
    ec2 = FakeEc2()
    monkeypatch.setattr(launch, "Ec2", lambda region: ec2)
    monkeypatch.setattr(launch, "_DryRunEc2", lambda region: ec2)
    monkeypatch.setattr(launch, "wait_for_ssh", lambda ip, **kw: True)
    argv = ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-1"]
    assert with_scope(launch.main, [*argv, "--dry-run"]) == 0
    assert ac.read_roster(str(path))[0]["host"] == ""
    assert with_scope(launch.main, argv) == 0
    row = ac.read_roster(str(path))[0]
    assert row["host"] == "ws-alice" and row["region"] == "us-west-2"
    assert sum(1 for c in ec2.calls if c[0] == "run_instance") == 1


def test_first_boot_merges_the_managed_settings(tmp_path):
    """AMI v3 (issue #36): the first boot adds the connector switch to an existing managed
    settings file without dropping what is there, and creates the file when it is missing.
    #882: and `CLAUDE_CODE_CERT_STORE=system`; a rerun changes nothing; a file that isn't a JSON
    object is refused, not overwritten. build.sh runs the same lines."""
    snippet = ac.COMMON_FIRST_BOOT.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    build = (ac_dir().parent / "ami" / "build.sh").read_text()
    assert build.split("managed-settings.json <<'PY'\n", 1)[1].split("\nPY\n", 1)[0] == snippet

    def merge(path) -> subprocess.CompletedProcess[str]:
        argv = [sys.executable, "-c", snippet, str(path)]
        return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603

    path = tmp_path / "managed-settings.json"
    path.write_text(json.dumps({"env": {"OTHER": "x"}, "permissions": {"deny": ["Bash(rm *)"]}}))
    assert merge(path).returncode == 0
    merged = {
        "env": {"OTHER": "x", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_CERT_STORE": "system"},
        "permissions": {"deny": ["Bash(rm *)"]},
        "disableClaudeAiConnectors": True,
    }
    assert json.loads(path.read_text()) == merged
    assert merge(path).returncode == 0 and json.loads(path.read_text()) == merged  # a rerun
    fresh = tmp_path / "new.json"
    assert merge(fresh).returncode == 0
    assert json.loads(fresh.read_text()) == {
        "env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_CERT_STORE": "system"},
        "disableClaudeAiConnectors": True,
    }
    for bad in ('{"env": {"OTHER": "x"}', "[]", '{"env": ["x"]}'):
        path.write_text(bad)
        assert merge(path).returncode != 0, bad
        assert path.read_text() == bad


# ---------------------------------------------------------- --admin-key (PM on #36, Mon 07:10)

# A real ed25519 public key (tests/workshop/test_admin_scan.py) under an organizer's name.
LAPTOP = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIw1k/2uq+sR/xmp2rpvzxiv4aHSdMJQPSmgtlFJv958 drew-laptop"
)


def test_parse_public_key_accepts_one_key_and_refuses_the_rest():
    assert ac.parse_public_key(f"# Drew's laptop\n{LAPTOP}\n") == LAPTOP
    assert ac.parse_public_key(LAPTOP.rsplit(" ", 1)[0]) == LAPTOP.rsplit(" ", 1)[0]
    bad = {
        "": "exactly one",
        f"{LAPTOP}\n{LAPTOP}": "exactly one",
        f'command="id" {LAPTOP}': "not an OpenSSH public key",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl\n-----END OPENSSH PRIVATE KEY-----": "exactly",
        "ssh-ed25519 not*base64": "base64",
        "ssh-rsa AAAAC3NzaC1lZDI1NTE5AAAAIIw1k/2uq+sR/xmp2rpvzxiv4aHSdMJQPSmgtlFJv958": "not a",
        LAPTOP.replace("drew-laptop", "x\x1brm"): "not printable ASCII",
        LAPTOP.replace("drew-laptop", "x" * 101): "at most 100",
        # codex on #144: the header alone, and a key cut short, are not keys
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5": "not a ssh-ed25519 key",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIw1k/2uq+sR/xmp2rpvzxiv4aHSdMJQPSmgtlFJv9": "base64",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIw1k/2uq+sR/xmp2rpvzxiv4aHSdMJQPSmgtlFJ": "not a",
    }
    for text, why in bad.items():
        with pytest.raises(ValueError, match=why):
            ac.parse_public_key(text, "k.pub")


def test_a_key_comment_may_contain_spaces():
    """PM nit on #144: `ssh-keygen -C "Drew's laptop"` is an ordinary key, not "not an OpenSSH
    public key". Whitespace runs become one space; the step stays shell-safe (quoted)."""
    key = LAPTOP.rsplit(" ", 1)[0]
    assert ac.parse_public_key(f"{key} Drew's  laptop\t(2026)") == f"{key} Drew's laptop (2026)"
    script = ac.admin_keys_script([f"{key} Drew's laptop; rm -rf /"])
    assert shlex.quote(f"{key} Drew's laptop; rm -rf /") in script


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
@pytest.mark.parametrize("kind", ["ed25519", "rsa", "ecdsa"])
def test_parse_public_key_agrees_with_ssh_keygen(tmp_path, kind):
    """Real keys of every common type pass whole and fail cut short; a cut key whose base64
    still decodes is refused by ssh-keygen too."""
    key = tmp_path / "k"
    keygen = shutil.which("ssh-keygen")
    subprocess.run(  # noqa: S603
        [keygen, "-q", "-t", kind, "-N", "", "-C", "drew-laptop", "-f", str(key)], check=True
    )
    pub = key.with_suffix(".pub").read_text().strip()
    assert ac.parse_public_key(pub) == pub
    kind_, data, _ = pub.split()
    cut = f"{kind_} {data[: (len(data) // 2) // 4 * 4]}"  # still valid base64
    with pytest.raises(ValueError, match="not a"):
        ac.parse_public_key(cut)
    (tmp_path / "cut.pub").write_text(cut + "\n")
    res = subprocess.run(  # noqa: S603
        [keygen, "-l", "-f", str(tmp_path / "cut.pub")], capture_output=True
    )
    assert res.returncode != 0


def test_admin_keys_go_into_participant_and_spare_user_data():
    keys = [LAPTOP]
    for row in (dict(ROW), ac.spare_rows(1)[0]):
        script = ac.render_user_data(row, keys)
        assert shlex.quote(LAPTOP) in script
        assert script.index("authorized_keys") < script.index("/etc/zab-participant.json")
        assert "authorized_keys" not in ac.render_user_data(row)
    with pytest.raises(ValueError):
        ac.render_user_data(dict(ROW), ["ssh-ed25519 nope"])


def test_the_admin_keys_step_appends_once_in_bash(tmp_path):
    """The rendered step, run by bash against a temporary home: the key goes in once, after a
    file without a final newline, and a rerun of the first boot adds nothing."""
    ssh_dir = tmp_path / "ssh"
    step = (
        ac.admin_keys_script([LAPTOP])
        .replace("/home/ubuntu/.ssh", str(ssh_dir))
        .replace(" -o ubuntu -g ubuntu", "")
    )
    ssh_dir.mkdir()
    ak = ssh_dir / "authorized_keys"
    ak.write_text("ssh-ed25519 AAAAkeypair drew-desktop")  # no final newline
    for _ in range(2):
        subprocess.run(["bash", "-c", step], check=True)  # noqa: S603, S607
    assert ak.read_text() == f"ssh-ed25519 AAAAkeypair drew-desktop\n{LAPTOP}\n"
    ak.unlink()
    subprocess.run(["bash", "-c", step], check=True)  # noqa: S603, S607
    assert ak.read_text() == f"{LAPTOP}\n"


def test_main_reads_and_checks_the_admin_key_files(tmp_path, monkeypatch, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email\nalice,alice@example.org\n")
    good, bad = tmp_path / "laptop.pub", tmp_path / "id_ed25519"
    good.write_text(LAPTOP + "\n")
    bad.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nxx\n-----END OPENSSH PRIVATE KEY-----\n")
    ec2 = FakeEc2()
    monkeypatch.setattr(launch, "Ec2", lambda region: ec2)
    monkeypatch.setattr(launch, "wait_for_ssh", lambda ip: True)
    base = ["--roster", str(roster), "--ami", "ami-1", "--security-group-id", "sg-1"]
    assert with_scope(launch.main, [*base, "--admin-key", str(bad)]) == 1
    assert "--admin-key" in capsys.readouterr().err and ec2.calls == []
    assert with_scope(launch.main, [*base, "--admin-key", str(good)]) == 0
    params = next(c[1] for c in ec2.calls if c[0] == "run_instance")
    assert shlex.quote(LAPTOP) in params["UserData"]


# ------------------------------------------------- host keys from the console (Mon 07:10)


def test_console_host_keys_reads_the_v3_block():
    text = console_text("SHA256:EtxHF/jFO11Cnz+kOh6Kq1k0", "SHA256:kg0tnmfaTJSKibFg7T4ldOo7k=")
    assert ac.console_host_keys(text) == [
        "SHA256:EtxHF/jFO11Cnz+kOh6Kq1k0",
        "SHA256:kg0tnmfaTJSKibFg7T4ldOo7k=",
    ]
    assert ac.console_host_keys("[ 1.0] booting\nSHA256:outside-the-block\n") == []
    half = "-----BEGIN SSH HOST KEY FINGERPRINTS-----\n256 SHA256:abc root@x (ED25519)\n"
    assert ac.console_host_keys(half) == []  # not finished printing yet


def test_the_real_client_asks_for_the_latest_console_output():
    seen = {}

    class Client:
        def get_console_output(self, **kw):
            seen.update(kw)
            return {"Output": "text"}

    ec2 = ac.Ec2.__new__(ac.Ec2)
    ec2._ec2 = Client()
    assert ec2.console_output("i-1") == "text"
    assert seen == {"InstanceId": "i-1", "Latest": True}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


@pytest.fixture(autouse=True)
def _spares_file(tmp_path, monkeypatch):
    """launch.main records spares in workshop/admin/out/: never the checkout's own in tests."""
    monkeypatch.setattr(launch, "SPARES_FILE", tmp_path / "out" / "spares.json")


def run_launch(rows, ec2, clock=None, **kw):
    clock = clock or Clock()
    summary = launch.Summary()
    launch.launch(
        rows, ec2, opts(**kw), log=quiet, ssh_ready=lambda ip: True, summary=summary,
        sleep=clock.sleep, clock=clock, spares=kw.pop("spares_rows", None),
    )  # fmt: skip
    return summary


def test_a_new_vm_gets_its_host_keys_recorded_after_the_console_catches_up():
    ec2 = FakeEc2()
    ec2.consoles["i-0001"] = ["", "[ 30.0] still booting", console_text("SHA256:new")]
    row = dict(ROW, host_keys="SHA256:of-a-previous-vm")
    clock = Clock()
    summary = run_launch([row], ec2, clock)
    assert row["host_keys"] == "SHA256:new" and ec2.console_reads == ["i-0001"] * 3
    assert clock.sleeps == [15.0, 15.0] and not summary.host_keys


def test_host_keys_that_never_show_are_reported_and_a_stale_value_is_cleared():
    ec2 = FakeEc2()
    ec2.consoles["i-0001"] = [""]
    row = dict(ROW, host_keys="SHA256:of-a-previous-vm")
    summary = run_launch([row], ec2)
    assert row["host_keys"] == ""  # the old VM's keys never vouch for the new one
    assert "not on the console after 900 s" in summary.host_keys["alice"]
    assert "host keys alice: not on the console" in summary.text()


def test_an_existing_vm_is_read_once_and_only_without_recorded_keys():
    ec2 = FakeEc2()
    existing = {"slug": "bob", "email": "bob@example.org", "repo": "", "ip": "", "host": ""}
    iid = existing_instance(ec2, "bob")
    ec2.consoles[iid] = [""]  # stopped and started since its first boot: the block is gone
    known = {"slug": "carol", "email": "c@example.org", "repo": "", "ip": "", "host": ""}
    existing_instance(ec2, "carol")
    known["host_keys"] = "SHA256:recorded"
    summary = run_launch([existing, known], ec2)
    assert ec2.console_reads == [iid]  # carol's keys are recorded already
    assert "stopped since its first boot" in summary.host_keys["bob"]
    assert known["host_keys"] == "SHA256:recorded" and not existing.get("host_keys")
    ec2.consoles[iid] = [console_text("SHA256:bob")]  # still there: recorded on a rerun
    run_launch([existing], ec2)
    assert existing["host_keys"] == "SHA256:bob"


def test_a_spares_host_keys_are_printed_for_the_hand_over():
    ec2 = FakeEc2()
    summary = run_launch([], ec2, spares_rows=ac.spare_rows(1))
    assert summary.host_keys["zab-spare-1"].startswith("SHA256:fpi0001 (copy into host_keys")


def test_a_dry_run_reads_no_console_for_a_vm_it_would_launch():
    ec2 = FakeEc2()
    row = dict(ROW)
    run_launch([row], ec2, dry_run=True)
    assert ec2.console_reads == [] and not row.get("host_keys")


# ---------------------------------------------------- PM nits on #144 (Mon 09:30, item 4)


def test_reused_vms_are_listed_when_admin_keys_cannot_reach_them():
    """--admin-key applies at first boot only: an existing VM is named, with its address."""
    ec2 = FakeEc2()
    existing_instance(ec2, "bob")
    bob = {"slug": "bob", "email": "bob@example.org", "repo": "", "ip": "", "host": ""}
    new = dict(ROW)
    summary = run_launch([bob, new], ec2, admin_keys=[LAPTOP])
    assert list(summary.admin_key_missed) == ["bob"]
    text = summary.text()
    assert "--admin-key applies at first boot only" in text and "bob (203.0.113." in text
    assert "may not have it" in text  # a rerun with the same key: they may have it (PM, 13:45)
    assert "cat >> ~/.ssh/authorized_keys" in text
    assert not run_launch([bob], ec2).admin_key_missed  # no --admin-key: nothing to say


def test_spares_are_recorded_under_out_for_the_hand_over(tmp_path):
    path = tmp_path / "out" / "spares.json"
    spares = ac.spare_rows(2)
    spares[0].update(ip="203.0.113.50", host_keys="SHA256:a SHA256:b", instance_id="i-1")
    spares[1].update(ip="")  # not launched: not recorded
    key = "nt-agents-2026/us-west-2/zab-spare-1"
    assert launch.record_spares(spares, path, "us-west-2", "nt-agents-2026") == [key]
    entry = json.loads(path.read_text())[key]
    assert entry["ip"] == "203.0.113.50" and entry["host_keys"] == "SHA256:a SHA256:b"
    assert entry["name"] == "zab-spare-1" and entry["instance_id"] == "i-1"
    assert entry["region"] == "us-west-2" and entry["updated"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    # a rerun whose console no longer shows the keys keeps them for the same instance only
    spares[0]["host_keys"] = ""
    other = {"slug": "zab-spare-2", "spare": "1", "ip": "203.0.113.51", "host_keys": "SHA256:c"}
    launch.record_spares([spares[0], other], path, "us-west-2", "nt-agents-2026")
    data = json.loads(path.read_text())
    assert data[key]["host_keys"] == "SHA256:a SHA256:b"
    assert data["nt-agents-2026/us-west-2/zab-spare-2"]["host_keys"] == "SHA256:c"
    # codex on #154: a replacement on the same (reused, tagged) Elastic IP is another VM
    spares[0]["instance_id"] = "i-2"
    launch.record_spares([spares[0]], path, "us-west-2", "nt-agents-2026")
    assert json.loads(path.read_text())[key]["host_keys"] == ""


def test_spares_in_two_regions_are_both_kept(tmp_path):
    """codex on #154: `--spares 1` in us-west-2 and in us-east-2 makes two `zab-spare-1`."""
    path = tmp_path / "spares.json"
    west = dict(ac.spare_rows(1)[0], ip="203.0.113.50", host_keys="SHA256:w", instance_id="i-w")
    east = dict(ac.spare_rows(1)[0], ip="198.51.100.7", host_keys="SHA256:e", instance_id="i-e")
    launch.record_spares([west], path, "us-west-2", "nt-agents-2026")
    launch.record_spares([east], path, "us-east-2", "nt-agents-2026")
    data = json.loads(path.read_text())
    assert data["nt-agents-2026/us-west-2/zab-spare-1"]["ip"] == "203.0.113.50"
    assert data["nt-agents-2026/us-east-2/zab-spare-1"]["ip"] == "198.51.100.7"


def test_main_records_the_spares_it_launched(tmp_path, monkeypatch):
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email\nalice,alice@example.org\n")
    ec2 = FakeEc2()
    monkeypatch.setattr(launch, "Ec2", lambda region: ec2)
    monkeypatch.setattr(launch, "wait_for_ssh", lambda ip: True)
    base = ["--roster", str(roster), "--ami", "ami-1", "--security-group-id", "sg-1"]
    assert with_scope(launch.main, [*base, "--spares", "1"]) == 0
    data = json.loads(launch.SPARES_FILE.read_text())
    key = "nt-agents-2026/us-west-2/zab-spare-1"
    assert list(data) == [key] and data[key]["host_keys"].startswith("SHA256:")
    assert data[key]["instance_id"].startswith("i-")


# ----------------------------------------------- the roster write-back (PM on #36, Mon 22:30)


def _launch_main_with(tmp_path, monkeypatch, during):
    """`launch.main` with a fake launch that records two VMs, and `during(path)` run while it
    "launches" (another script writing the roster meanwhile)."""
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)

    def fake_launch(rows, ec2, opts, *, summary, spares):
        for i, row in enumerate(rows):
            row.update(host=f"ws-{row['slug']}", ip=f"203.0.113.{10 + i}", host_keys=f"SHA256:k{i}")
        during(path)

    monkeypatch.setattr(launch, "launch", fake_launch)
    monkeypatch.setattr(launch, "Ec2", lambda region: object())
    monkeypatch.setattr(launch, "check_security_group", lambda ec2, sg, **kw: None)
    monkeypatch.setattr(launch, "SPARES_FILE", tmp_path / "out" / "spares.json")
    rc = with_scope(
        launch.main, ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-1"]
    )
    return rc, path


def test_launch_writes_back_only_its_columns_into_the_roster_as_it_is_now(tmp_path, monkeypatch):
    """A launch takes 20 to 40 minutes; what another script wrote meanwhile (a `github_invite`
    from the `--invites-only` loop, a `zulip_user_id`) survives the launch's write-back."""

    def meanwhile(path):
        rows = ac.read_roster(str(path))
        rows[0]["zulip_user_id"] = "101"
        rows[1]["notes"] = "edited by hand"
        ac.write_roster(str(path), rows)

    rc, path = _launch_main_with(tmp_path, monkeypatch, meanwhile)
    assert rc == 0
    after = ac.read_roster(str(path))
    assert after[0]["zulip_user_id"] == "101" and after[1]["notes"] == "edited by hand"
    assert [r["host"] for r in after] == [f"ws-{r['slug']}" for r in after]
    assert after[1]["host_keys"] == "SHA256:k1" and after[0]["ip"] == "203.0.113.10"


def test_a_roster_broken_meanwhile_is_not_written_and_the_changes_are_printed(
    tmp_path, monkeypatch, capsys
):
    """PM nit on #166: a roster that no longer reads is left as it is, the run says to fix it
    and rerun, and prints what it would have recorded (the host keys above all)."""

    def break_it(path):
        path.write_text(path.read_text() + "zab-spare-9,x,y,z@example.org\n")

    rc, path = _launch_main_with(tmp_path, monkeypatch, break_it)
    assert rc == 1
    err = capsys.readouterr().err
    assert "roster NOT written" in err and "rerun launch.py" in err
    assert "host_keys=SHA256:k1" in err and "ws-" in err
    assert "SHA256:k1" not in path.read_text()
    # and once more after the summary, the last thing printed (PM on #36, Tue 09:15)
    assert err.rstrip().splitlines()[-1].startswith("roster NOT written (see the changes")


# ------------------------------------------------------- kept VMs: protect.py (issue #207)

TAG = ("workshop", "nt-agents-2026")


def test_every_instance_stops_on_shutdown_and_protect_adds_protection():
    params = launch.instance_params(fresh_row(), opts())
    assert params["InstanceInitiatedShutdownBehavior"] == "stop"
    assert "DisableApiTermination" not in params
    assert launch.instance_params(fresh_row(), opts(protect=True))["DisableApiTermination"] is True


def test_protect_turns_protection_on_and_shutdown_to_stop_once():
    ec2 = populated()
    ec2.attributes["i-0001"][1] = "terminate"
    log: list[str] = []
    assert protect.protect(ec2, TAG, log=log.append) == 0
    assert ec2.attributes == {"i-0001": [True, "stop"], "i-0003": [True, "stop"]}
    assert log == [
        "alice i-0001 (running): set termination protection on; on shutdown: terminate → stop",
        "bob-2 i-0003 (running): set termination protection on",
        "reading it back until EC2 shows it, for 300 s at most",
        "alice i-0001: termination protection on: set; EC2 shows it now",
        "bob-2 i-0003: termination protection on: set; EC2 shows it now",
    ]
    ec2.calls.clear()
    log.clear()
    assert protect.protect(ec2, TAG, log=log.append) == 0  # idempotent: reads only
    assert {c[0] for c in ec2.calls} == {"find_instances", "protection"}
    assert all("unchanged (protected, stops on shutdown)" in line for line in log)


def test_protect_off_lifts_protection_but_keeps_stop_on_shutdown():
    ec2 = populated()
    protect.protect(ec2, TAG, log=quiet)
    ec2.attributes["i-0003"][1] = "terminate"
    assert protect.protect(ec2, TAG, only=["bob-2"], off=True, log=quiet) == 0
    assert ec2.attributes == {"i-0001": [True, "stop"], "i-0003": [False, "stop"]}


def test_protect_dry_run_changes_nothing():
    ec2 = populated()
    log: list[str] = []
    assert protect.protect(ec2, TAG, dry_run=True, log=log.append) == 0
    assert not any(c[0].startswith("set_") for c in ec2.calls)
    assert log[0] == "alice i-0001 (running): would set termination protection on"
    assert log[-1] == "dry run: nothing changed"


def test_protect_reports_an_unknown_slug_and_changes_nothing():
    ec2 = populated()
    log: list[str] = []
    assert protect.protect(ec2, TAG, only=["carol"], log=log.append) == 1
    assert log == ["carol: no live instance with workshop=nt-agents-2026"]
    assert not any(c[0].startswith("set_") for c in ec2.calls)


def test_a_failure_on_one_instance_does_not_stop_the_others():
    ec2 = populated()
    ec2.broken.add("i-0001")
    log: list[str] = []
    assert protect.protect(ec2, TAG, log=log.append) == 1
    assert log[0] == "alice i-0001: FAILED: UnauthorizedOperation"
    assert ec2.attributes["i-0003"] == [True, "stop"]


def test_teardown_keeps_protected_vms_and_their_addresses():
    ec2 = populated()
    protect.protect(ec2, TAG, only=["alice"], log=quiet)
    ec2.calls.clear()
    log: list[str] = []
    assert teardown.teardown(ec2, TAG, region="us-west-2", yes=True, log=log.append) == 0
    assert [c for c in ec2.calls if c[0] == "terminate"] == [("terminate", ["i-0003"])]
    assert list(ec2.addresses) == ["eipalloc-0002"]  # alice's address stays with her VM
    assert ec2.instances["i-0001"]["State"]["Name"] == "running"
    assert log[0].startswith("keep i-0001 ws-alice: termination protection is on")
    assert "protect.py --off --only alice" in log[0]


def test_strict_teardown_fails_on_a_vm_still_protected():
    """`--strict` (`!retire`, #559): a protected VM is never torn down, and fails the run; the
    others go, with their addresses."""
    ec2 = populated()
    protect.protect(ec2, TAG, only=["alice"], log=quiet)
    ec2.calls.clear()
    log: list[str] = []
    assert (
        teardown.teardown(ec2, TAG, region="us-west-2", yes=True, strict=True, log=log.append) == 1
    )
    assert [c for c in ec2.calls if c[0] == "terminate"] == [("terminate", ["i-0003"])]
    assert list(ec2.addresses) == ["eipalloc-0002"]
    assert ec2.instances["i-0001"]["State"]["Name"] == "running"
    refused = [line for line in log if line.startswith("refused i-0001 ws-alice (participant")]
    assert refused and "--strict: every VM selected must go" in refused[0]
    ec2.calls.clear()  # alone: nothing else to do, and still a failure
    assert (
        teardown.teardown(
            ec2, TAG, region="us-west-2", only=["alice"], yes=True, strict=True, log=quiet
        )
        == 1
    )
    assert not any(c[0] in ("terminate", "release_address") for c in ec2.calls)
    # its dry run lists it `keep`, and passes: the plan shows it
    assert (
        teardown.teardown(
            ec2, TAG, region="us-west-2", only=["alice"], dry_run=True, strict=True, log=quiet
        )
        == 0
    )
    protect.protect(ec2, TAG, only=["alice"], off=True, log=quiet)
    assert (
        teardown.teardown(
            ec2, TAG, region="us-west-2", only=["alice"], yes=True, strict=True, log=quiet
        )
        == 0
    )
    assert ec2.instances["i-0001"]["State"]["Name"] == "shutting-down" and not ec2.addresses
    assert (
        teardown.teardown(
            ec2, TAG, region="us-west-2", only=["alice"], yes=True, strict=True, log=quiet
        )
        == 0
    )


def test_teardown_with_only_protected_vms_changes_nothing():
    ec2 = populated()
    protect.protect(ec2, TAG, log=quiet)
    ec2.calls.clear()
    assert (
        teardown.teardown(ec2, TAG, region="us-west-2", confirm=lambda q: pytest.fail(q), log=quiet)
        == 0
    )
    assert not any(c[0] in ("terminate", "release_address") for c in ec2.calls)


# ------------------------------- the admin server's flags and guard (ADMIN.md §3.2, issue #311)

#: The admin server tagged with the fleet's scope by mistake: it has no `participant` tag.
SERVER_TAGS = {"Name": "zab-admin", "workshop": "nt-agents-2026", "zab-admin": "nt"}


def add_server(ec2: FakeEc2, tags: dict[str, str] = SERVER_TAGS) -> tuple[str, str]:
    """An instance with `tags` and its associated Elastic IP: (instance id, allocation id)."""
    spec = [{"ResourceType": "instance", "Tags": ac.tag_list(tags)}]
    iid = ec2.run_instance(TagSpecifications=spec)
    aid = ec2.allocate_address(ac.tag_list(tags))
    ec2.associate_address(aid, iid)
    ec2.attributes[iid] = [True, "stop"]  # its own termination protection
    ec2.calls.clear()
    return iid, aid


def test_has_participant_and_only_slugs():
    assert ac.has_participant({"Tags": ac.tag_list({"participant": "alice"})})
    for tags in ({}, {"participant": ""}, {"participant": "  "}, {"Name": "ws-alice"}):
        assert not ac.has_participant({"Tags": ac.tag_list(tags)}), tags
    assert ac.only_slugs(None) == [] and ac.only_slugs([]) == []
    assert ac.only_slugs("alice") == ["alice"]  # the form before --only was repeatable
    assert ac.only_slugs(["bob", "alice", "bob"]) == ["bob", "alice"]


def test_teardown_never_selects_what_has_no_participant_tag():
    """The scope tags alone (no `--only`) find the server, a blank-tagged VM and a free address
    with the server's tags (allocated, not yet associated): all are listed as `skip`, never
    asked about, terminated or released; dry run and real run alike."""
    for dry_run in (True, False):
        ec2 = populated()
        server, server_ip = add_server(ec2)
        blank, blank_ip = add_server(ec2, {**SERVER_TAGS, "Name": "ws-x", "participant": " "})
        free = ec2.allocate_address(ac.tag_list(SERVER_TAGS))  # only the address guard stops it
        # M17 (review of #316): on alice's VM, which the run terminates, still not released
        on_alice = ec2.allocate_address(ac.tag_list(SERVER_TAGS))
        ec2.associate_address(on_alice, "i-0001")
        ec2.calls.clear()
        log: list[str] = []
        assert (
            teardown.teardown(
                ec2, TAG, region="us-west-2", dry_run=dry_run, yes=True, log=log.append
            )
            == 0
        )
        lines = log[0].splitlines()
        assert lines[:6] == [
            "skip i-0005 zab-admin: no participant tag (not a participant's VM or a spare)",
            "skip i-0007 ws-x: no participant tag (not a participant's VM or a spare)",
            "skip eipalloc-0006 203.0.113.6: no participant tag",
            "skip eipalloc-0008 203.0.113.8: no participant tag",
            f"skip {free} 203.0.113.9: no participant tag",
            f"skip {on_alice} 203.0.113.10: no participant tag",
        ]
        assert [line.split()[1] for line in lines[6:]] == [
            "i-0001", "i-0003", "eipalloc-0002", "eipalloc-0004"
        ]  # fmt: skip
        assert ("release_address", on_alice) not in ec2.calls and on_alice in ec2.addresses
        assert ("protection", server) not in ec2.calls and ("protection", blank) not in ec2.calls
        for iid, aid in ((server, server_ip), (blank, blank_ip)):
            assert ec2.instances[iid]["State"]["Name"] == "running"
            assert ec2.addresses[aid]["InstanceId"] == iid
        assert free in ec2.addresses and ("release_address", free) not in ec2.calls
        terminated = [c for c in ec2.calls if c[0] == "terminate"]
        released = [c[1] for c in ec2.calls if c[0] == "release_address"]
        if dry_run:
            assert not terminated and not released and log[-1] == "dry run: nothing changed"
        else:
            assert terminated == [("terminate", ["i-0001", "i-0003"])]
            assert released == ["eipalloc-0002", "eipalloc-0004"]


def test_a_zab_admin_tag_wins_over_a_participant_tag_in_teardown_and_protect():
    """Defence in depth for a manual run, where no IAM deny applies: the server, tagged as
    alice by mistake, is still never selected, by the scope alone or by `--only alice`."""
    for only in (None, ["alice"]):
        ec2 = populated()
        server, server_ip = add_server(ec2, {**SERVER_TAGS, "participant": "alice"})
        log: list[str] = []
        assert (
            teardown.teardown(ec2, TAG, region="us-west-2", only=only, yes=True, log=log.append)
            == 0
        )
        lines = log[0].splitlines()
        assert f"skip {server} zab-admin: tagged zab-admin (not a participant's VM or a spare)" in (
            lines
        )
        assert f"skip {server_ip} 203.0.113.6: tagged zab-admin" in lines
        assert ("protection", server) not in ec2.calls and server_ip in ec2.addresses
        assert ec2.instances[server]["State"]["Name"] == "running"
        assert ("terminate", ["i-0001"] + ([] if only else ["i-0003"])) in ec2.calls
    ec2 = populated()
    server, _ = add_server(ec2, {**SERVER_TAGS, "participant": "alice"})
    for only in ((), ["alice"]):
        log = []
        assert protect.protect(ec2, TAG, only=only, off=True, log=log.append) == 0
        assert ec2.attributes[server] == [True, "stop"] and ("protection", server) not in ec2.calls
    assert log == ["alice i-0001 (running): unchanged (not protected, stops on shutdown)"]


def test_a_zab_admin_tag_wins_over_a_participant_tag_in_launch():
    ec2 = FakeEc2()
    server, server_ip = add_server(
        ec2, {**SERVER_TAGS, **launch.participant_tags("alice", FLEET_SCOPE)}
    )
    free = ec2.allocate_address(
        ac.tag_list({**launch.participant_tags("alice", FLEET_SCOPE), "zab-admin": "x"})
    )
    ec2.calls.clear()
    out = launch.launch([fresh_row()], ec2, opts(), log=quiet, ssh_ready=lambda ip: True)
    kinds = [c[0] for c in ec2.calls]
    assert kinds.count("run_instance") == 1 and kinds.count("allocate_address") == 1
    assert ec2.addresses[server_ip]["InstanceId"] == server
    assert "InstanceId" not in ec2.addresses[free]
    theirs = (ec2.addresses[server_ip]["PublicIp"], ec2.addresses[free]["PublicIp"])
    assert out[0]["ip"] not in theirs


def test_a_failed_lookup_is_one_line_in_teardown_and_protect():
    class Down(FakeEc2):
        def find_instances(self, tags, states):
            raise RuntimeError("ExpiredToken: the token expired\nRequest ID: 1")

    for run in (
        lambda log: teardown.teardown(Down(), TAG, region="us-west-2", yes=True, log=log),
        lambda log: protect.protect(Down(), TAG, log=log),
    ):
        log: list[str] = []
        assert run(log.append) == 1
        assert log == ["FAILED: the EC2 lookup: RuntimeError: ExpiredToken: the token expired"]


def test_teardown_with_only_the_server_in_scope_changes_nothing():
    ec2 = FakeEc2()
    add_server(ec2)
    log: list[str] = []
    rc = teardown.teardown(
        ec2, TAG, region="us-west-2", confirm=lambda q: pytest.fail(q), log=log.append
    )
    assert rc == 0 and log[0].startswith("skip i-0001 zab-admin")
    assert not any(c[0] in ("terminate", "release_address") for c in ec2.calls)


def test_teardown_keeps_an_address_attached_to_an_instance_it_does_not_terminate():
    """A participant-tagged address on the server (or on any VM outside this run) stays. In a
    default VPC, `ReleaseAddress` would silently disassociate it from that instance and release
    it; in another VPC, AWS would refuse it partway through the list."""
    ec2 = populated()
    server, _ = add_server(ec2)
    stray = ec2.allocate_address(ac.tag_list(launch.participant_tags("alice", FLEET_SCOPE)))
    ec2.associate_address(stray, server)
    ec2.calls.clear()
    log: list[str] = []
    assert (
        teardown.teardown(ec2, TAG, region="us-west-2", only=["alice"], yes=True, log=log.append)
        == 0
    )
    want = f"skip {stray} 203.0.113.7: attached to {server}, which this run does not terminate"
    assert want in log[0].splitlines()
    assert [c for c in ec2.calls if c[0] == "terminate"] == [("terminate", ["i-0001"])]
    assert [c[1] for c in ec2.calls if c[0] == "release_address"] == ["eipalloc-0002"]
    assert ec2.addresses[stray]["InstanceId"] == server


def test_teardown_keeps_an_address_associated_with_a_network_interface_alone():
    """Associated with an interface that has no instance, an address has an `AssociationId` and
    no `InstanceId` (the `Address` shape): it is not free, and is never released."""
    ec2 = populated()
    eni = ec2.allocate_address(ac.tag_list(launch.participant_tags("alice", FLEET_SCOPE)))
    ec2.addresses[eni].update(AssociationId="eipassoc-1", NetworkInterfaceId="eni-1")
    ec2.calls.clear()
    log: list[str] = []
    assert (
        teardown.teardown(ec2, TAG, region="us-west-2", only=["alice"], yes=True, log=log.append)
        == 0
    )
    want = f"skip {eni} 203.0.113.5: attached to eni-1, which this run does not terminate"
    assert want in log[0].splitlines()
    assert [c[1] for c in ec2.calls if c[0] == "release_address"] == ["eipalloc-0002"]
    assert eni in ec2.addresses


def test_teardown_only_is_repeatable_and_looks_up_each_slug():
    ec2 = populated()
    add_server(ec2)
    spare = ac.tag_list(launch.participant_tags("zab-spare-1", FLEET_SCOPE))
    iid = ec2.run_instance(TagSpecifications=[{"ResourceType": "instance", "Tags": spare}])
    ec2.calls.clear()
    only = ["alice", "zab-spare-1", "alice"]
    assert teardown.teardown(ec2, TAG, region="us-west-2", only=only, yes=True, log=quiet) == 0
    lookups = [c[1] for c in ec2.calls if c[0] in ("find_instances", "find_addresses")]
    scope = {"workshop": "nt-agents-2026"}
    by_slug = [{**scope, "participant": "alice"}, {**scope, "participant": "zab-spare-1"}]
    assert lookups == by_slug * 2  # instances, then addresses; each slug once, by its own tag
    assert [c for c in ec2.calls if c[0] == "terminate"] == [("terminate", ["i-0001", iid])]
    assert ec2.instances["i-0003"]["State"]["Name"] == "running"  # bob-2 was not named


def test_teardown_main_takes_only_repeatedly(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(teardown, "Ec2", lambda region: object())
    monkeypatch.setattr(teardown, "teardown", lambda ec2, tag, **kw: seen.update(kw) or 0)
    assert with_scope(teardown.main, ["--only", "alice", "--only", "zab-spare-1", "--dry-run"]) == 0
    assert seen["only"] == ["alice", "zab-spare-1"] and seen["dry_run"] is True
    assert with_scope(teardown.main, []) == 0 and seen["only"] == []  # no --only: the whole scope


def test_protect_never_touches_an_instance_without_a_participant_tag():
    """`protect.py --off` must not lift the protection of a server tagged `workshop=…`."""
    ec2 = populated()
    server, _ = add_server(ec2)
    log: list[str] = []
    assert protect.protect(ec2, TAG, off=True, log=log.append) == 0
    assert log[0] == (
        "skip i-0005 zab-admin: no participant tag (not a participant's VM or a spare)"
    )
    assert ec2.attributes[server] == [True, "stop"] and ("protection", server) not in ec2.calls
    assert [line.split()[1] for line in log[1:]] == ["i-0001", "i-0003"]
    for only in (["alice"], "alice"):  # a bare string is one slug, as in teardown
        log.clear()
        assert protect.protect(ec2, TAG, only=only, log=log.append) == 0
        assert log[0].startswith("alice i-0001")  # with --only: no skips
        assert all("i-0001" in line or line.startswith("reading it back") for line in log)


def test_launch_never_adopts_or_attaches_what_has_no_participant_tag():
    """Even named like her VM, the server is not taken for alice's, and neither its address nor
    a free one with the workshop tag alone is attached to her new VM."""
    ec2 = FakeEc2()
    server, server_ip = add_server(ec2, {**SERVER_TAGS, "Name": "ws-alice"})
    free = ec2.allocate_address(ac.tag_list({"workshop": "nt-agents-2026"}))
    ec2.calls.clear()
    out = launch.launch([fresh_row()], ec2, opts(), log=quiet, ssh_ready=lambda ip: True)
    assert [c[0] for c in ec2.calls].count("run_instance") == 1
    assert ec2.addresses[server_ip]["InstanceId"] == server
    assert "InstanceId" not in ec2.addresses[free]
    theirs = (ec2.addresses[server_ip]["PublicIp"], ec2.addresses[free]["PublicIp"])
    assert out[0]["ip"] and out[0]["ip"] not in theirs
    for call in ec2.calls:
        if call[0] in ("find_instances", "find_addresses"):
            assert call[1]["participant"] == "alice"


def test_launch_only_is_repeatable_across_rows_and_spares(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    summary = launch.Summary()
    launch.launch(
        ac.read_roster(str(path)),
        FakeEc2(),
        opts(only=["bob-2", "zab-spare-2"]),
        log=quiet,
        ssh_ready=lambda ip: True,
        summary=summary,
        spares=ac.spare_rows(2),
    )
    assert summary.launched == ["bob-2", "zab-spare-2"]


def test_main_refuses_an_unknown_only_before_any_aws_call(tmp_path, monkeypatch, capsys):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    base = ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-1"]

    def no_aws(region):
        raise ImportError("no AWS in this test")  # tolerated by a dry run only

    monkeypatch.setattr(launch, "Ec2", no_aws)
    for extra in (["--only", "alice", "--only", "carol"], ["--only", "zab-spare-1"]):
        assert with_scope(launch.main, [*base, *extra]) == 1
        assert "no such roster row or spare" in capsys.readouterr().err
    target = tmp_path / "state" / "spares.json"
    argv = [*base, "--dry-run", "--spares", "1", "--spares-file", str(target)]
    assert with_scope(launch.main, [*argv, "--only", "alice", "--only", "zab-spare-1"]) == 0
    out = capsys.readouterr().out
    assert "ws-alice" in out and "ws-zab-spare-1" in out and "ws-bob-2" not in out
    assert not target.exists() and path.read_text() == ROSTER  # a dry run writes nothing


def test_main_records_spares_in_the_spares_file_given(tmp_path, monkeypatch, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text("slug,email\nalice,alice@example.org\n")
    monkeypatch.setattr(launch, "Ec2", lambda region: FakeEc2())
    monkeypatch.setattr(launch, "wait_for_ssh", lambda ip: True)
    target = tmp_path / "state" / "spares.json"
    base = ["--roster", str(roster), "--ami", "ami-1", "--security-group-id", "sg-1"]
    assert with_scope(launch.main, [*base, "--spares", "1", "--spares-file", str(target)]) == 0
    assert list(json.loads(target.read_text())) == ["nt-agents-2026/us-west-2/zab-spare-1"]
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert not launch.SPARES_FILE.exists()
    assert f"address and host keys in {target}" in capsys.readouterr().out


# ------------------------------------------------------------- a released address, given again


#: Alice's and bob-2's VMs at `populated()`'s addresses, carol's elsewhere (#604).
ADDRESSED = """slug,email,host,ip,host_keys,zulip_user_id
alice,alice@example.org,ws-alice,203.0.113.2,SHA256:a,101
bob-2,bob@example.org,ws-bob-2,203.0.113.4,SHA256:b,102
carol,carol@example.org,ws-carol,198.51.100.9,SHA256:c,103
"""


def test_teardown_clears_the_ip_of_the_rows_whose_address_it_released(tmp_path):
    """#604: with `--roster`, a row whose address was released loses its `ip`, and keeps its
    `host` (launch.py leaves it alone); the others, and the other columns, are as they were.
    A dry run, and a VM kept protected with its address, change nothing."""
    path = tmp_path / "roster.csv"
    path.write_text(ADDRESSED)
    ec2 = populated()
    ec2.attributes["i-0001"][0] = True  # alice's VM is protected: kept, with its address
    log: list[str] = []
    scope = ("workshop", "nt-agents-2026")
    assert (
        teardown.teardown(
            ec2, scope, region="us-west-2", dry_run=True, roster=str(path), log=log.append
        )
        == 0
    )
    assert path.read_text() == ADDRESSED
    assert (
        teardown.teardown(
            ec2, scope, region="us-west-2", yes=True, roster=str(path), log=log.append
        )
        == 0
    )
    rows = {r["slug"]: r for r in ac.read_roster(str(path))}
    assert [rows[s]["ip"] for s in ("alice", "bob-2", "carol")] == ["203.0.113.2", "",
                                                                     "198.51.100.9"]  # fmt: skip
    assert rows["bob-2"]["host"] == "ws-bob-2" and rows["bob-2"]["zulip_user_id"] == "102"
    assert "roster: bob-2's ip 203.0.113.4 cleared (released; its host stays)" in log
    assert list(ec2.addresses) == ["eipalloc-0002"]  # alice's


def test_teardown_says_which_rows_to_clear_when_the_roster_doesnt_read(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ADDRESSED + "alice,again@example.org,,,,\n")  # a duplicate slug
    log: list[str] = []
    rc = teardown.teardown(populated(), ("workshop", "nt-agents-2026"), only="bob-2", yes=True,
                           region="us-west-2", roster=str(path), log=log.append)  # fmt: skip
    assert rc == 1 and any("clear by hand the ip of its rows at 203.0.113.4" in s for s in log)


class Refusing(FakeEc2):
    """`populated()`'s EC2, refusing to terminate the VMs in `protected` (EC2's lag, #378) and
    to release the addresses in `kept`: what teardown.py doesn't release, it must not clear."""

    def __init__(self, protected: tuple[str, ...] = (), kept: tuple[str, ...] = ()) -> None:
        super().__init__()
        source = populated()
        self.instances, self.addresses, self.attributes = (
            source.instances, source.addresses, source.attributes,
        )  # fmt: skip
        self.protected, self.kept = set(protected), set(kept)

    def terminate(self, ids):
        self.calls.append(("terminate", list(ids)))
        for iid in ids:
            if iid in self.protected:
                exc = RuntimeError(f"An error occurred (OperationNotPermitted): {iid}")
                exc.response = {"Error": {"Code": "OperationNotPermitted", "Message": iid}}
                raise exc
        for iid in ids:
            self.instances[iid]["State"]["Name"] = "shutting-down"

    def release_address(self, aid):
        if aid in self.kept:
            raise RuntimeError(f"An error occurred (InvalidIPAddress.InUse): {aid}")
        super().release_address(aid)


@pytest.mark.parametrize("ec2_kw", [{"kept": ("eipalloc-0004",)}, {"protected": ("i-0003",)}])
def test_teardown_clears_only_the_addresses_it_released(tmp_path, ec2_kw):
    """Review of #606: bob-2's address isn't released, its release refused or its VM still
    refused as protected after the retries: bob-2 keeps his `ip`, alice's is cleared, exit 1."""
    path = tmp_path / "roster.csv"
    path.write_text(ADDRESSED)
    clock, log = Clock(), []
    scope = ("workshop", "nt-agents-2026")
    rc = teardown.teardown(Refusing(**ec2_kw), scope, yes=True, roster=str(path),
                           region="us-west-2", log=log.append, sleep=clock.sleep,
                           clock=clock)  # fmt: skip
    rows = {r["slug"]: r for r in ac.read_roster(str(path))}
    assert rc == 1 and (rows["alice"]["ip"], rows["bob-2"]["ip"]) == ("", "203.0.113.4"), log


def test_a_launch_that_fails_after_the_address_still_clears_the_stale_row():
    """Review of #606: the address is recorded, then the SSH wait raises; the row that kept the
    address still loses it (`launch()`'s `finally`)."""
    rows = [
        {"slug": "zab-test-a", "email": "a@example.org", "host": "ws-zab-test-a",
         "ip": "203.0.113.176"},
        {"slug": "zab-test-r", "email": "r@example.org", "host": "", "ip": ""},
    ]  # fmt: skip

    def broken(ip):
        raise OSError("no route")

    summary = launch.Summary()
    launch.launch(rows, Recycling("203.0.113.176"), opts(only=["zab-test-r"]), log=quiet,
                  ssh_ready=broken, summary=summary)  # fmt: skip
    assert "zab-test-r" in summary.failed and rows[0]["ip"] == "" and "zab-test-a" in summary.stale


def test_a_row_at_an_existing_spares_address_is_reported_not_cleared():
    """Review of #606: the hand-over by hand (ORGANIZERS §7.2) gave bob zab-spare-1's address and
    host keys, and left the VM tagged as the spare. `!launch all spares=2` reports bob, and leaves
    his row alone; a spare launched in this run clears nothing of his."""
    ec2 = FakeEc2()
    iid = existing_instance(ec2, "zab-spare-1")
    aid = ec2.allocate_address(ac.tag_list(launch.participant_tags("zab-spare-1", FLEET_SCOPE)))
    ip = ec2.associate_address(aid, iid)
    bob = {"slug": "bob", "email": "b@example.org", "host": "ws-bob", "ip": ip,
           "host_keys": "SHA256:spare1"}  # fmt: skip
    summary = run_launch([bob], ec2, spares=2, spares_rows=ac.spare_rows(2), region="us-west-2")
    assert bob["ip"] == ip and summary.stale == {}
    assert summary.held["bob"].startswith(f"holds zab-spare-1's address {ip}: a spare handed")
    assert f"check bob: holds zab-spare-1's address {ip}" in summary.text()
    assert "zab-spare-2" in summary.launched


class Recycling(FakeEc2):
    """EC2 that gives the next address allocated the one in `again`, as it gave zab-test-a's,
    released by A9 step 6, to zab-test-r on Oct 23 (#604)."""

    def __init__(self, again: str) -> None:
        super().__init__()
        self.again = again

    def allocate_address(self, tags):
        aid = super().allocate_address(tags)
        self.addresses[aid]["PublicIp"] = self.again
        return aid


def test_a_launch_given_a_released_address_clears_it_from_the_row_that_kept_it(tmp_path):
    """#604: the row whose VM was torn down (before `teardown.py --roster`) kept its `ip`. The
    new VM's row gets it, the stale row loses it and keeps its `host`, and the roster written
    back is one that every admin script reads again."""
    import admin_common

    rows = [
        {"slug": "zab-test-a", "email": "a@example.org", "host": "ws-zab-test-a",
         "ip": "203.0.113.176"},
        {"slug": "zab-test-r", "email": "r@example.org", "host": "", "ip": ""},
    ]  # fmt: skip
    ec2 = Recycling("203.0.113.176")
    summary = run_launch(rows, ec2, only=["zab-test-r"])
    assert [r["ip"] for r in rows] == ["", "203.0.113.176"]
    assert rows[0]["host"] == "ws-zab-test-a" and summary.launched == ["zab-test-r"]
    assert summary.stale == {"zab-test-a": "its ip 203.0.113.176 is on zab-test-r's VM now "
                             "(released with its own, given again): cleared"}  # fmt: skip
    assert "stale ip zab-test-a: its ip 203.0.113.176" in summary.text()
    admin_common.check_duplicates(rows)  # what refused the roster on Oct 23
    with pytest.raises(ValueError, match=r"duplicate ip .*launch.py --only <the row whose VM"):
        admin_common.check_duplicates([dict(rows[0], ip="203.0.113.176"), rows[1]])


def test_a_conditional_change_applies_only_while_the_file_has_the_value(tmp_path):
    """Codex on #606: `expect` clears a stale `ip` only while the roster still holds it."""
    path = tmp_path / "roster.csv"
    path.write_text(ADDRESSED)
    changes = {"alice": {"ip": ""}, "bob-2": {"ip": "", "host_keys": "SHA256:new"}}
    expect = {"alice": {"ip": "203.0.113.2"}, "bob-2": {"ip": "198.51.100.1"}}
    assert ac.merge_roster_changes(str(path), changes, expect) == []
    rows = {r["slug"]: r for r in ac.read_roster(str(path))}
    assert rows["alice"]["ip"] == ""  # still the stale value: cleared
    assert (rows["bob-2"]["ip"], rows["bob-2"]["host_keys"]) == ("203.0.113.4", "SHA256:new")


def test_a_launch_keeps_an_address_written_meanwhile_on_the_row_it_cleared(tmp_path, monkeypatch):
    """Codex on #606: zab-test-a's stale address went to zab-test-r, and a spare was handed over
    to zab-test-a by hand while the launch ran. The launch's write clears zab-test-a's `ip` only
    if the file still has the stale one, so the spare's address stays."""
    path = tmp_path / "roster.csv"
    path.write_text("slug,email,host,ip\nzab-test-a,a@example.org,ws-zab-test-a,203.0.113.176\n"
                    "zab-test-r,r@example.org,,\n")  # fmt: skip

    def fake_launch(rows, ec2, opts, *, summary, spares):
        stale, new = rows
        new.update(host="ws-zab-test-r", ip="203.0.113.176")
        summary.addresses["zab-test-r"] = "203.0.113.176"
        launch._forget_stale(rows, "zab-test-r", summary, quiet)
        assert stale["ip"] == "" and summary.cleared == {"zab-test-a": "203.0.113.176"}
        text = path.read_text().replace("ws-zab-test-a,203.0.113.176", "ws-zab-test-a,198.51.100.7")
        path.write_text(text)  # zab-test-a's hand-over, by hand, meanwhile

    monkeypatch.setattr(launch, "launch", fake_launch)
    monkeypatch.setattr(launch, "Ec2", lambda region: object())
    monkeypatch.setattr(launch, "check_security_group", lambda ec2, sg, **kw: None)
    assert (
        with_scope(
            launch.main, ["--roster", str(path), "--ami", "ami-1", "--security-group-id", "sg-1"]
        )
        == 0
    )
    rows = {r["slug"]: r for r in ac.read_roster(str(path))}
    assert (rows["zab-test-a"]["ip"], rows["zab-test-r"]["ip"]) == (
        "198.51.100.7",
        "203.0.113.176",
    )


def test_a_launch_unsticks_a_roster_where_two_rows_have_one_address():
    """#604: a roster stuck already. A run for the row whose VM EC2 shows the address on clears
    the other's; a run for the stale row alone (no VM, its host recorded: left alone) and a
    dry run clear nothing, since neither learns from EC2 where the address is."""
    ec2 = FakeEc2()
    iid = existing_instance(ec2, "zab-test-r")
    aid = ec2.allocate_address(ac.tag_list(launch.participant_tags("zab-test-r", FLEET_SCOPE)))
    ip = ec2.associate_address(aid, iid)

    def stuck():
        return [
            {"slug": "zab-test-a", "email": "a@example.org", "host": "ws-zab-test-a", "ip": ip},
            {"slug": "zab-test-r", "email": "r@example.org", "host": "ws-zab-test-r", "ip": ip,
             "host_keys": "SHA256:r"},
        ]  # fmt: skip

    for kw in (
        {"only": ["zab-test-a"], "region": "us-west-2"},  # its host: left alone
        {"only": ["zab-test-r"], "dry_run": True},
    ):
        rows = stuck()
        assert run_launch(rows, ec2, **kw).stale == {} and rows == stuck(), kw
    rows = stuck()
    summary = run_launch(rows, ec2, only=["zab-test-r"])
    assert [r["ip"] for r in rows] == ["", ip] and set(summary.stale) == {"zab-test-a"}
    assert summary.existing == ["zab-test-r"] and "run_instance" not in [c[0] for c in ec2.calls]


def test_a_rows_ip_written_meanwhile_is_not_cleared_by_teardown(tmp_path, monkeypatch):
    """#611's criterion 5: `forget`'s `expect` guard. bob-2's address is released; by then the
    file has another `ip` for bob-2 (a hand-over meanwhile), which stays. Without the guard,
    the write would clear it."""
    path = tmp_path / "roster.csv"
    path.write_text(ADDRESSED.replace("203.0.113.4", "198.51.100.44"))  # the file, now
    read = ac.read_roster  # what teardown.py read before the write: bob-2 at the released one
    monkeypatch.setattr(teardown, "read_roster", lambda p: [
        {**r, "ip": "203.0.113.4"} if r["slug"] == "bob-2" else r for r in read(p)
    ])  # fmt: skip
    assert teardown.forget(str(path), {"203.0.113.4"}, quiet)
    rows = {r["slug"]: r for r in ac.read_roster(str(path))}
    assert rows["bob-2"]["ip"] == "198.51.100.44" and rows["alice"]["ip"] == "203.0.113.2"


# ----------------------------------------------- spares.json after a teardown, a launch (#607)

W2 = "nt-agents-2026/us-west-2"


def spares_file(tmp_path, entries: dict) -> str:
    path = tmp_path / "spares.json"
    path.write_text(json.dumps(entries))
    return str(path)


def test_teardown_drops_the_spares_json_entries_at_the_addresses_it_released(tmp_path):
    """#607: a spare torn down, and an adopted one's entry once its row's VM is, go; an entry
    at an address kept (a protected VM), another region's or another scope's, stays; written
    atomically, 0600. A dry run changes nothing."""
    ec2 = populated()  # alice's VM at 203.0.113.2, bob-2's at 203.0.113.4
    ec2.attributes["i-0001"][0] = True  # alice's is protected: kept, with its address
    entries = {
        f"{W2}/zab-spare-1": {"name": "zab-spare-1", "ip": "203.0.113.4", "adopted_by": "bob-2"},
        f"{W2}/zab-spare-2": {"name": "zab-spare-2", "ip": "203.0.113.2"},
        "nt-agents-2026/us-east-2/zab-spare-1": {"name": "zab-spare-1", "ip": "203.0.113.4"},
        "other-event/us-west-2/zab-spare-1": {"name": "zab-spare-1", "ip": "203.0.113.4"},
    }
    path = spares_file(tmp_path, entries)
    scope, log = ("workshop", "nt-agents-2026"), []
    assert (
        teardown.teardown(
            ec2, scope, region="us-west-2", dry_run=True, spares_file=path, log=log.append
        )
        == 0
    )
    assert json.loads(open(path).read()) == entries
    rc = teardown.teardown(ec2, scope, yes=True, spares_file=path, region="us-west-2",
                           log=log.append)  # fmt: skip
    assert rc == 0, log
    left = json.loads(open(path).read())
    assert sorted(left) == sorted(set(entries) - {f"{W2}/zab-spare-1"})
    assert f"spares.json: {W2}/zab-spare-1 removed (its address 203.0.113.4 released)" in log
    assert oct((tmp_path / "spares.json").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("ec2_kw", [{"kept": ("eipalloc-0004",)}, {"protected": ("i-0003",)}])
def test_teardown_drops_only_the_spares_whose_address_it_released(tmp_path, ec2_kw):
    """#607's "only for addresses actually released": a release refused, or a VM still refused
    as protected, keeps its spare's entry."""
    entries = {f"{W2}/zab-spare-1": {"name": "zab-spare-1", "ip": "203.0.113.4"},
               f"{W2}/zab-spare-2": {"name": "zab-spare-2", "ip": "203.0.113.2"}}  # fmt: skip
    path, clock = spares_file(tmp_path, entries), Clock()
    rc = teardown.teardown(Refusing(**ec2_kw), ("workshop", "nt-agents-2026"), yes=True,
                           region="us-west-2", spares_file=path, log=quiet, sleep=clock.sleep,
                           clock=clock)  # fmt: skip
    assert rc == 1 and sorted(json.loads(open(path).read())) == [f"{W2}/zab-spare-1"]


def test_a_spares_file_teardown_cant_read_is_named_and_fails_the_run(tmp_path):
    log = []
    assert teardown.forget_spares(str(tmp_path / "none.json"), {"203.0.113.4"}, f"{W2}/", quiet)
    bad = tmp_path / "spares.json"
    bad.write_text("{")
    assert not teardown.forget_spares(str(bad), {"203.0.113.4"}, f"{W2}/", log.append)
    assert "remove by hand its entries at 203.0.113.4" in log[0] and bad.read_text() == "{"
    rc = teardown.teardown(populated(), ("workshop", "nt-agents-2026"), yes=True,
                           region="us-west-2", spares_file=str(bad), log=quiet)  # fmt: skip
    assert rc == 1


def test_teardown_takes_the_spares_file_on_its_command_line(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(teardown, "Ec2", lambda region: "ec2")
    monkeypatch.setattr(teardown, "teardown", lambda ec2, tag, **kw: seen.update(kw) or 0)
    argv = ["--tag", "workshop=x", "--region", "us-east-2", "--spares-file", "s.json", "--yes"]
    assert with_scope(teardown.main, argv) == 0
    assert (seen["spares_file"], seen["region"]) == ("s.json", "us-east-2")


def test_record_spares_keeps_the_same_instances_fields_and_drops_a_stale_entry(tmp_path):
    """#611 in #607: for the same instance, `adopted_by` (the mark of a hand-over by hand) and
    any other field stay; a new instance gets a fresh entry. Another entry of the scope and
    region at the address recorded, with another instance, goes; another region's stays."""
    path = tmp_path / "spares.json"
    path.write_text(json.dumps({
        f"{W2}/zab-spare-1": {"name": "zab-spare-1", "instance_id": "i-1", "ip": "203.0.113.5",
                              "host_keys": "SHA256:a", "adopted_by": "alice", "note": "kept"},
        f"{W2}/zab-spare-2": {"name": "zab-spare-2", "instance_id": "i-old", "ip": "203.0.113.6"},
        "nt-agents-2026/us-east-2/zab-spare-2": {"name": "zab-spare-2", "ip": "203.0.113.6"},
    }))  # fmt: skip
    one = dict(ac.spare_rows(1)[0], ip="203.0.113.5", instance_id="i-1", host_keys="")
    log: list[str] = []
    launch.record_spares([one], path, "us-west-2", "nt-agents-2026", log=log.append)
    entry = json.loads(path.read_text())[f"{W2}/zab-spare-1"]
    assert (entry["adopted_by"], entry["note"], entry["host_keys"]) == ("alice", "kept", "SHA256:a")
    # zab-spare-3, launched now, at zab-spare-2's address, released with its VM: that one goes
    three = dict(ac.spare_rows(3)[2], ip="203.0.113.6", instance_id="i-3", host_keys="SHA256:c")
    launch.record_spares([three], path, "us-west-2", "nt-agents-2026", log=log.append)
    data = json.loads(path.read_text())
    assert f"{W2}/zab-spare-2" not in data and data[f"{W2}/zab-spare-3"]["ip"] == "203.0.113.6"
    assert "nt-agents-2026/us-east-2/zab-spare-2" in data  # another region: never the same
    assert log == [f"spares.json: {W2}/zab-spare-2 dropped: its address 203.0.113.6 is "
                   f"{W2}/zab-spare-3's now"]  # fmt: skip
    one["instance_id"] = "i-9"  # a replacement: none of the old instance's fields
    launch.record_spares([one], path, "us-west-2", "nt-agents-2026", log=log.append)
    entry = json.loads(path.read_text())[f"{W2}/zab-spare-1"]
    assert "adopted_by" not in entry and "note" not in entry and entry["host_keys"] == ""
