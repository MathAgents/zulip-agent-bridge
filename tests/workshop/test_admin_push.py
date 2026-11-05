"""`workshop/admin/push_config.py` (workshop/admin/README.md §2, §6) against a fake SSH runner."""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Mapping

import admin_common as ac
import idle_stop
import push_config as pc
import pytest
import status as st
import tools_list as tm

from .test_sage_wrapper import Vm

STAGING = "/tmp/zab-push.AbC123"  # noqa: S108 - the fake VM's mktemp answer

ROSTER = """github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes
alice,Alice,Smith,alice@example.org,alice/thesis,1,ws-alice,203.0.113.5,101,
bob,Bob,Jones,bob@example.org,sandbox,2,ws-bob,203.0.113.7,102,
"""
ALICE = "203.0.113.5"
BOB = "203.0.113.7"
KH = "/tmp/kh test"  # noqa: S108 - a known_hosts path with a space, for the quoting check
#: The `sage` wrapper's step (#482), and its answer on a VM that has the wrapper already
SAGE_STEP = "sage_wrapper.py install "
SAGE_UNCHANGED = "sage: unchanged: /home/agent/.local/bin/sage is the wrapper\n"


def owner_stdout(github: str, *, spare: str = "", hostname: str | None = None) -> str:
    record = {"github": github, "repo": "x"} if not spare else {"github": "", "spare": spare}
    host = hostname if hostname is not None else (f"ws-{github}" if not spare else f"ws-{spare}")
    m = pc.OWNER_MARK
    return f"{m}JSON\n{pc.json.dumps(record)}\n\n{m}HOST\n{host}\n{m}END\n"


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / "roster.csv"
    path.write_text(ROSTER)
    return path


@pytest.fixture
def out_dir(tmp_path):
    out = tmp_path / "out"
    for github in ("alice", "bob"):
        for kind in ("codex", "claude"):
            p = out / github / f"{kind}.zuliprc"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"[api]\nemail={kind}-{github}-bot@org\nkey={'K' * 32}\nsite=https://org\n"
            )
    return out


class FakeRunner:
    """Records argv; answers the fixed remote commands; fails where `fail` says so.

    `owners` maps an IP to what its VM answers to the owner check (default: the VM of the login
    the test's roster gives that IP, so the check passes).
    """

    def __init__(
        self,
        *,
        fail: dict[str, str] | None = None,
        bad_hosts: set[str] | None = None,
        owners: dict[str, str] | None = None,
        answers: dict[str, str] | None = None,
    ):
        # substring of the command → stdout; by default the `sage` wrapper is in place (#482)
        self.answers = {SAGE_STEP: SAGE_UNCHANGED, **(answers or {})}
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        self.fail = fail or {}  # substring of the command → stderr
        self.bad_hosts = bad_hosts or set()
        self.owners = owners if owners is not None else {}

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.calls.append(list(argv))
        self.stdins.append(stdin)
        if len(argv) < 3:
            return ac.RunResult(0, "", "")
        target = argv[-1] if argv[0] == "scp" else argv[-3]
        host = target.split("@", 1)[1].split(":", 1)[0]
        if host in self.bad_hosts:
            return ac.RunResult(255, "", "ssh: connect to host: Connection timed out")
        remote = argv[-1]
        for needle, err in self.fail.items():
            if needle in remote:
                return ac.RunResult(1, "", err)
        if argv[0] == "ssh" and remote == pc.OWNER_CMD:
            if host in self.owners:
                return ac.RunResult(0, self.owners[host], "")
            login = {ALICE: "alice", BOB: "bob"}.get(host, "alice")
            return ac.RunResult(0, owner_stdout(login), "")
        if argv[0] == "ssh" and remote == pc.MKTEMP_CMD:
            return ac.RunResult(0, STAGING + "\n", "")
        if argv[0] == "ssh" and remote == pc.SERVICE_INSTALL_CMD:
            return ac.RunResult(
                0,
                'Installed zab.service\n[{"name": "config", "ok": true}, '
                '{"name": "codex:connection", "ok": true, "warning": true}]\n',
                "",
            )
        for needle, out in self.answers.items():
            if needle in remote:
                return ac.RunResult(0, out, "")
        return ac.RunResult(0, "", "")

    def remotes(self, host: str, *, holds: bool = False) -> list[str]:
        """The remote commands sent to `host`; the idle-stop hold around a rollout (PM on #218)
        is left out unless `holds`, and asserted on its own below."""
        hold = (pc.IDLE_HOLD_ON_CMD, pc.IDLE_HOLD_OFF_CMD)
        return [
            c[-1]
            for c in self.calls
            if c[0] == "ssh" and c[-3] == f"ubuntu@{host}" and (holds or c[-1] not in hold)
        ]


# ------------------------------------------------------------------------------ config.toml


def test_render_config_toml_round_trips_and_matches_the_profile(tmp_path):
    row = {"github": "alice", "repo": "alice/thesis", "zulip_user_id": "101"}
    text = pc.render_config_toml(row)
    data = tomllib.loads(text)
    assert data["bridge"] == {
        "machine": "alice",
        "default_project": "~/work/thesis",
        "max_concurrent_turns": 4,
        "mention_after_s": 120,  # SPEC §4.8, the bridge's default unless the workshop's (#711)
        "default_workspace": "worktree",  # each topic its own worktree (issue #36, Mon 18:15)
        "allow_public_channels": True,  # public channels allowed (Drew, issue #36, Tue 06:30)
        "service_path_prepend": ["~/venv/bin", "~/.elan/bin"],
        "service_env": {"VIRTUAL_ENV": "~/venv"},
    }
    assert [b["name"] for b in data["bots"]] == ["codex", "claude"]
    assert all(b["allow_user_ids"] == [101] for b in data["bots"])
    assert data["bots"][0]["zuliprc"] == "~/.config/zab/codex.zuliprc"
    assert data["backends"]["codex"]["defaults"] == {
        "approval": "never",
        "sandbox": "danger-full-access",
        "effort": "high",
    }
    assert data["backends"]["codex"]["connect"] == "daemon"
    assert data["backends"]["codex"]["binary"] == "/home/agent/.local/bin/codex"
    assert data["backends"]["claude"]["defaults"] == {
        "approval": "bypassPermissions",
        "effort": "high",
    }
    assert data["backends"]["claude"]["accounts"] == {"main": "~/.claude"}
    # A's loader accepts it (SPEC §5.2 schema)
    from zab.config import load_config

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(text)
    cfg = load_config(cfg_path)
    assert cfg.bridge.machine == "alice" and cfg.bridge.max_concurrent_turns == 4
    home = pathlib.Path.home()
    assert cfg.bridge.service_path_prepend == [str(home / "venv/bin"), str(home / ".elan/bin")]
    assert cfg.bridge.service_env == {"VIRTUAL_ENV": str(home / "venv")}
    assert {b.name for b in cfg.bots} == {"codex", "claude"}


def test_render_config_requires_zulip_user_id():
    with pytest.raises(ValueError, match="zulip_user_id"):
        pc.render_config_toml({"github": "alice", "repo": "x", "zulip_user_id": ""})


def test_parse_doctor_finds_the_json_list():
    assert pc.parse_doctor('Installed\n[{"name": "a", "ok": true}]\n') == [
        {"name": "a", "ok": True}
    ]
    assert pc.parse_doctor("no json here\n") is None
    assert pc.parse_doctor("[not json\n") is None


# ----------------------------------------------------------------------------- rollout


def test_rollout_vm_sequence_and_quoting(out_dir, monkeypatch):
    monkeypatch.delenv("ZAB_REPO", raising=False)  # the public repository's URL, below
    runner = FakeRunner()
    row = {
        "github": "alice",
        "repo": "alice/thesis",
        "zulip_user_id": "101",
        "host": "ws-alice.example.org",  # the alias is never used for the connection
        "ip": ALICE,
    }
    opts = pc.Options(tag="v0.1.0rc1", out_dir=out_dir, known_hosts=KH)
    result = pc.rollout_vm(row, opts, runner)
    assert result.ok and result.installed and result.doctor_ok and not result.failures
    assert result.host == ALICE
    assert [c.get("name") for c in result.doctor] == ["config", "codex:connection"]
    # every ssh call is a fixed argument vector with the command as the single last argument,
    # addressed to the roster's IP, with the host key pinned and nothing forwarded
    ssh_calls = [c for c in runner.calls if c[0] == "ssh"]
    options = ac.ssh_options(KH)
    for c in ssh_calls:
        assert c[1 : 1 + len(options)] == options
        assert c[-3] == f"ubuntu@{ALICE}" and c[-2] == "--"
        assert len(c) == len(options) + 4
    assert "StrictHostKeyChecking=yes" in options and "ForwardAgent=no" in options
    assert (
        'UserKnownHostsFile="/tmp/kh test"' in options
        and "GlobalKnownHostsFile=/dev/null" in options
    )
    assert "accept-new" not in " ".join(options)
    assert not any("ws-alice" in part for c in runner.calls for part in c)
    remotes = runner.remotes(ALICE)
    # the VM's own record is read before anything else, then the staging directory
    assert remotes[0] == pc.OWNER_CMD and remotes[1] == pc.MKTEMP_CMD
    # eleven files scp'd into the staging directory, in order, with the same pinned options:
    # the tools list's two (#475) and the sage wrapper's two (#482) with the instructions, the
    # warm-up's two (#461) after them
    scps = [c for c in runner.calls if c[0] == "scp"]
    assert [c[-1] for c in scps] == [
        f"ubuntu@{ALICE}:{STAGING}/{name}"
        for name in (
            "config.toml",
            "codex.zuliprc",
            "claude.zuliprc",
            "agent-instructions.md",
            "agent_instructions.py",
            "tools_list.py",
            "tools-nt-agents-2026-v3.json",
            "sage_wrapper.py",
            "sage",
            "warm_cache.py",
            "zab-warm-cache.service",
        )
    ]
    assert scps[1][-2] == str(out_dir / "alice" / "codex.zuliprc")
    assert all(c[1 : 1 + len(options)] == options for c in scps)
    # files installed 0600 owner agent, then uv tool install from the tag, then the service step
    assert (
        "sudo install -d -o agent -g agent -m 0700 /home/agent/.config/zab && "
        f"sudo install -o agent -g agent -m 0600 {STAGING}/codex.zuliprc "
        "/home/agent/.config/zab/codex.zuliprc && "
    ) in remotes[2]
    assert remotes[2].endswith(f"{STAGING}/config.toml /home/agent/.config/zab/config.toml")
    # the upgrade runs as agent from / (ssh starts in the unreadable /home/ubuntu; round 3)
    assert remotes[3] == (
        "sudo -u agent -H bash -lc 'cd / && /home/agent/.local/bin/uv tool install --force "
        "git+https://github.com/MathAgents/zulip-agent-bridge@v0.1.0rc1'"
    )
    assert remotes[1].startswith('d=$(mktemp -d /tmp/zab-push.XXXXXX) && chmod 0755 "$d"')
    assert remotes[4] == pc.SERVICE_INSTALL_CMD
    assert "XDG_RUNTIME_DIR=/run/user/$uid" in remotes[4]
    # the upgraded binary must be the one running before doctor reports (codex round 2)
    assert remotes[4].endswith(
        f"'cd / && {{ {pc.SENTINEL_OFF}; zab service install && systemctl --user restart "
        "zab.service || echo ---ZAB-SERVICE-FAILED; }; zab doctor --service-env --json'"
    )
    assert scps[-3][-2] == str(pc.SAGE_WRAPPER) and pc.SAGE_WRAPPER.is_file()
    # the image's `sage` link replaced by the new images' wrapper, as agent, where it is that
    # link (#482): here the wrapper is there already
    assert remotes[5] == (
        f"sudo chmod 0644 {STAGING}/sage_wrapper.py {STAGING}/sage && sudo -u agent -H "
        "bash -lc "
        + shlex.quote(f"cd / && /usr/bin/python3 {STAGING}/sage_wrapper.py install {STAGING}/sage")
    )
    # the VM's list of its software where the image has none, as root (#475), with the
    # SageMath entry for the wrapper found there
    assert remotes[6] == (
        f"sudo /usr/bin/python3 {STAGING}/tools_list.py install "
        f"{STAGING}/tools-nt-agents-2026-v3.json --with sage-wrapper"
    )
    # last, the agents' instructions, spliced as agent by the uploaded script (PM on #36, Mon
    # 19:00; last since Mon 22:30, so that this file can never hold up the rest), their tools
    # section made from that list by the script beside it
    text, script = f"{STAGING}/agent-instructions.md", f"{STAGING}/agent_instructions.py"
    # the instructions name lean-project: a VM without it is noted (review on #261); and
    # whether the workspace's Mathlib is shared, for --lean-shared-default (#704)
    packages = "/home/agent/lean/workspace/.lake/packages"
    probe = (
        " && { test -x /usr/local/bin/lean-project || echo ---ZAB-NO-LEAN-PROJECT; "
        f"if sudo test -L {packages}; then :; elif sudo test -d {packages}; "
        "then echo ---ZAB-LEAN-NOT-SHARED; else echo ---ZAB-NO-LEAN-WORKSPACE; fi; }"
    )
    assert remotes[7] == (
        f"sudo chmod 0644 {text} {script} {STAGING}/tools_list.py && sudo -u agent -H "
        "bash -lc " + shlex.quote(f"cd / && /usr/bin/python3 {script} {text}") + probe
    )
    # then the page-cache warm-up after each boot (#461), and staging is removed
    assert remotes[8] == pc.warm_on_cmd(STAGING)
    assert remotes[9] == f"rm -rf -- {STAGING}"
    assert not result.notes  # the wrapper was there already: nothing to say


# ------------------------------------------------------------------ the VM's own owner record


def test_owner_mismatch_copies_nothing(out_dir):
    """PM review of PR #47 (1): the roster said this IP is alice's, the VM says it is bob's."""
    runner = FakeRunner(owners={ALICE: owner_stdout("bob")})
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert not result.ok and not result.installed
    assert result.failures == [
        "VM belongs to 'bob' (hostname 'ws-bob'), not 'alice': nothing copied; "
        "check the roster's ip column"
    ]
    assert [c[0] for c in runner.calls] == ["ssh"]  # the owner check only: no scp, no install
    # a VM without any record is refused too
    runner = FakeRunner(
        owners={
            ALICE: f"{pc.OWNER_MARK}JSON\n\n{pc.OWNER_MARK}HOST\nip-10-0-0-1\n{pc.OWNER_MARK}END\n"
        }
    )
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert "VM belongs to '(no record)'" in result.failures[0] and len(runner.calls) == 1
    # the record matches but the hostname does not: refused as well
    runner = FakeRunner(owners={ALICE: owner_stdout("alice", hostname="ws-carol")})
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert result.failures == ["VM hostname is 'ws-carol', expected ws-alice: nothing copied"]
    # unparseable output never passes
    runner = FakeRunner(owners={ALICE: "garbage\n"})
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert result.failures == ["owner check: unexpected output"] and len(runner.calls) == 1
    # login comparison ignores case (GitHub logins are case-insensitive; the roster may differ)
    runner = FakeRunner(owners={ALICE: owner_stdout("Alice")})
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert result.ok


def test_spare_needs_adopt_spare_which_rewrites_its_record(out_dir):
    """A spare VM (ORGANIZERS §7.2) is accepted only for a row named in --adopt-spare, and its
    record and hostname are rewritten for the new owner before anything is copied."""
    runner = FakeRunner(owners={ALICE: owner_stdout("", spare="zab-spare-1")})
    row = {"github": "alice", "repo": "alice/thesis", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert result.failures == [
        "VM is the spare 'zab-spare-1' (hostname 'ws-zab-spare-1'); "
        "use --adopt-spare alice to hand it over"
    ]
    assert len(runner.calls) == 1
    runner = FakeRunner(owners={ALICE: owner_stdout("", spare="zab-spare-1")})
    opts = pc.Options(out_dir=out_dir, adopt_spares=frozenset({"alice"}))
    row = {**row, "first_name": "Alice", "last_name": "O'Neil", "email": "alice@example.org"}
    result = pc.rollout_vm(row, opts, runner)
    assert result.ok and result.adopted == "zab-spare-1"
    remotes = runner.remotes(ALICE)
    assert remotes[0] == pc.OWNER_CMD
    assert (
        remotes[2]
        == "sudo tee /etc/zab-participant.json >/dev/null && sudo hostnamectl set-hostname ws-alice"
    )
    assert runner.stdins[2] == '{"slug": "alice", "repo": "thesis"}'  # no login (issue #36)
    # agent's git identity, as at a participant's first boot (PM on #36, Wed 21:20), quoted;
    # before the owner record, so that a failure leaves a spare to retry (codex on #195)
    set_name = shlex.join(["git", "config", "--global", "user.name", "Alice O'Neil"])
    assert shlex.split(remotes[1]) == [
        *("sudo", "-u", "agent", "-H", "bash", "-lc"),
        f"cd / && {set_name}",
        "&&",
        *("sudo", "-u", "agent", "-H", "bash", "-lc"),
        "cd / && git config --global user.email alice@example.org",
    ]
    assert remotes[3] == pc.MKTEMP_CMD
    assert "was zab-spare-1" in pc.render([result])
    # --adopt-spare does not loosen the check for a VM that belongs to someone else
    runner = FakeRunner(owners={ALICE: owner_stdout("bob")})
    result = pc.rollout_vm(row, opts, runner)
    assert "VM belongs to 'bob'" in result.failures[0] and len(runner.calls) == 1


def test_a_failed_git_identity_leaves_the_spare_to_retry(out_dir):
    """codex on #195: the identity comes first; if it fails, the VM is still a spare, so the
    same --adopt-spare run can simply be repeated."""
    runner = FakeRunner(
        owners={ALICE: owner_stdout("", spare="zab-spare-1")},
        fail={"user.email": "fatal: could not lock config file"},
    )
    opts = pc.Options(out_dir=out_dir, adopt_spares=frozenset({"alice"}))
    row = {"slug": "alice", "repo": "x", "email": "alice@example.org", "ip": ALICE}
    result = pc.rollout_vm(row, opts, runner)
    assert result.failures[0].startswith("adopt spare: git identity")
    # the owner record was not rewritten (the owner check only reads it)
    assert not any(
        r.startswith("sudo tee /etc/zab-participant.json") for r in runner.remotes(ALICE)
    )


def test_adopting_a_spare_needs_the_rows_e_mail(out_dir):
    """Without an e-mail there is no git identity to give: nothing is changed on the spare."""
    runner = FakeRunner(owners={ALICE: owner_stdout("", spare="zab-spare-1")})
    opts = pc.Options(out_dir=out_dir, adopt_spares=frozenset({"alice"}))
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, opts, runner)
    assert result.failures == ["adopt spare: alice: no e-mail for git's user.email"]
    assert runner.remotes(ALICE) == [pc.OWNER_CMD]


def test_parse_owner():
    o = pc.parse_owner(owner_stdout("alice"))
    assert (o.slug, o.spare, o.hostname) == ("alice", "", "ws-alice")
    o = pc.parse_owner(owner_stdout("", spare="zab-spare-2"))
    assert (o.slug, o.spare, o.hostname) == ("", "zab-spare-2", "ws-zab-spare-2")
    with pytest.raises(ValueError, match="not JSON"):
        pc.parse_owner(
            f"{pc.OWNER_MARK}JSON\n{{bad\n\n{pc.OWNER_MARK}HOST\nh\n{pc.OWNER_MARK}END\n"
        )
    with pytest.raises(ValueError, match="not an object"):
        pc.parse_owner(f"{pc.OWNER_MARK}JSON\n[1]\n\n{pc.OWNER_MARK}HOST\nh\n{pc.OWNER_MARK}END\n")


def test_rollout_vm_with_wheel_quotes_the_path(out_dir, tmp_path):
    wheel = tmp_path / "zulip agent-0.1.0rc1-py3-none-any.whl"
    wheel.write_bytes(b"not really a wheel")
    runner = FakeRunner()
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(wheel=str(wheel), out_dir=out_dir), runner)
    assert result.ok
    scps = [c for c in runner.calls if c[0] == "scp" and c[-2].endswith(".whl")]
    assert scps[-1][-2] == str(wheel)
    assert scps[-1][-1] == f"ubuntu@{ALICE}:{STAGING}/zulip agent-0.1.0rc1-py3-none-any.whl"
    upgrade = runner.remotes(ALICE)[3]
    quoted = shlex.quote(f"{STAGING}/zulip agent-0.1.0rc1-py3-none-any.whl")
    inner = shlex.quote(f"cd / && /home/agent/.local/bin/uv tool install --force {quoted}")
    assert upgrade == f"sudo chmod 0644 {quoted} && sudo -u agent -H bash -lc {inner}"
    # the inner command, once the outer shell has unquoted it, is itself well quoted
    assert shlex.split(upgrade)[-1] == (
        f"cd / && /home/agent/.local/bin/uv tool install --force {quoted}"
    )
    assert "git+" not in upgrade


def test_a_tag_installs_from_zab_repo_or_else_the_public_repository(out_dir, monkeypatch):
    """#593: one default, the public repository, whose tags are the releases; `ZAB_REPO`
    overrides it, as it does build.sh's, whose default is the same."""
    monkeypatch.delenv("ZAB_REPO", raising=False)
    assert pc.repo_url() == pc.REPO_URL == "https://github.com/MathAgents/zulip-agent-bridge"
    build = (pathlib.Path(pc.__file__).parents[1] / "ami" / "build.sh").read_text()
    assert f'\nZAB_REPO="${{ZAB_REPO:-{pc.REPO_URL}}}"\n' in build
    monkeypatch.setenv("ZAB_REPO", "https://git.example.org/fork/zab")
    runner = FakeRunner()
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    assert pc.rollout_vm(row, pc.Options(tag="v9.9.9", out_dir=out_dir), runner).ok
    assert (
        "uv tool install --force git+https://git.example.org/fork/zab@v9.9.9"
        in (runner.remotes(ALICE)[3])
    )


def test_rollout_vm_stops_on_failure_but_cleans_up(out_dir):
    runner = FakeRunner(fail={"tool install": "error: resolution failed"})
    row = {"github": "bob", "repo": "sandbox", "zulip_user_id": "102", "ip": BOB}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert not result.ok and not result.installed and result.doctor_ok is None
    assert result.failures == ["uv tool install: error: resolution failed"]
    remotes = runner.remotes(BOB)
    assert pc.SERVICE_INSTALL_CMD not in remotes and remotes[-1] == f"rm -rf -- {STAGING}"


def test_rollout_vm_rejects_a_strange_staging_dir(out_dir):
    class Weird(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[-1] == pc.OWNER_CMD:
                return super().__call__(argv, **kw)
            self.calls.append(list(argv))
            return ac.RunResult(0, "/etc; rm -rf /\n", "")

    runner = Weird()
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert "unexpected staging directory" in result.failures[0]
    assert len(runner.calls) == 2  # owner check, mktemp; nothing else was attempted


def test_rollout_vm_doctor_failures_are_listed(out_dir):
    class Doctor(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[0] == "ssh" and argv[-1] == pc.SERVICE_INSTALL_CMD:
                self.calls.append(list(argv))
                return ac.RunResult(
                    0, '[{"name": "claude:login", "ok": false, "detail": "not logged in"}]\n', ""
                )
            return super().__call__(argv, **kw)

    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), Doctor())
    assert result.installed and result.doctor_ok is False
    assert result.failures == ["doctor: claude:login: not logged in"]


def test_rollout_vm_missing_inputs(tmp_path):
    runner = FakeRunner()
    row = {"github": "alice", "repo": "x", "zulip_user_id": "", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=tmp_path / "none"), runner)
    assert not runner.calls  # nothing was contacted
    assert any("missing codex zuliprc" in f for f in result.failures)
    assert any("zulip_user_id" in f for f in result.failures)
    # the alias in `host` is not an address: without `ip` nothing is contacted
    row = {"github": "x", "repo": "x", "zulip_user_id": "1", "host": "ws-x", "ip": ""}
    result = pc.rollout_vm(row, pc.Options(), runner)
    assert "no IP address for 'x'" in result.failures[0] and not runner.calls


def test_uninstall_runs_the_service_command_and_removes_the_warm_up(out_dir):
    runner = FakeRunner()
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir, uninstall=True), runner)
    assert result.ok
    assert runner.remotes(ALICE) == [pc.SERVICE_UNINSTALL_CMD, pc.WARM_OFF_CMD]
    assert pc.SERVICE_UNINSTALL_CMD.endswith(
        f"'cd / && {{ {pc.SENTINEL_OFF}; zab service uninstall; }}'"
    )


@pytest.mark.parametrize("first_line", ["# Managed by zab service install", "# mine"])
def test_sentinel_off_removes_only_the_unit_zab_wrote(tmp_path, first_line):
    """Before the installed release's `zab service install` or `uninstall` (#204): a release
    without the sentinel, or its uninstall, must not leave one answering DMs. Run as the VM
    would, with `systemctl` a stub on PATH that records its arguments."""
    import subprocess

    bin_dir, units = tmp_path / "bin", tmp_path / "home/.config/systemd/user"
    bin_dir.mkdir()
    units.mkdir(parents=True)
    log = tmp_path / "systemctl.log"
    stub = bin_dir / "systemctl"
    stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
    stub.chmod(0o755)
    unit = units / "zab-sentinel.service"
    unit.write_text(first_line + "\n[Unit]\n")
    other = units / "zab.service"
    other.write_text("# Managed by zab service install\n")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    argv = ["bash", "-c", pc.SENTINEL_OFF]
    done = subprocess.run(argv, env=env, capture_output=True, text=True)  # noqa: S603
    assert done.returncode == 0, done.stderr
    managed = first_line.startswith("# Managed")
    assert unit.exists() is not managed and other.exists()
    calls = log.read_text().splitlines() if log.exists() else []
    expected = ["--user disable --now zab-sentinel.service", "--user daemon-reload"]
    assert calls == (expected if managed else [])
    unit.unlink(missing_ok=True)
    again = subprocess.run(argv, env=env, capture_output=True, text=True)  # noqa: S603
    assert again.returncode == 0 and not again.stderr  # no unit: nothing to do, quietly


# ---------------------------------------------------------------------------------- CLI


def test_main_isolates_failures_and_exits_nonzero(roster, out_dir, monkeypatch, capsys):
    runner = FakeRunner(bad_hosts={BOB})
    monkeypatch.setattr(pc, "subprocess_runner", runner)
    code = pc.main(
        [
            *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
            *("--parallel", "2", "--known-hosts", KH),
        ]
    )
    assert code == 1
    out = capsys.readouterr()
    lines = [line for line in out.out.splitlines() if line.startswith(("alice", "bob"))]
    assert lines[0].startswith("alice") and " ok " in lines[0]
    assert lines[1].startswith("bob") and "FAIL" in lines[1] and "ssh:" in lines[1]
    assert "1 VM(s) failed" in out.err
    assert "K" * 32 not in out.out + out.err
    assert runner.remotes(ALICE)[-1].startswith("rm -rf")  # alice completed
    assert all(f'UserKnownHostsFile="{KH}"' in c for c in runner.calls)


def test_main_refuses_duplicate_rows_before_contacting_anything(
    roster, out_dir, monkeypatch, capsys
):
    """PM review of PR #47 (1): two rows with one IP would put both participants' keys on one VM."""
    runner = FakeRunner()
    monkeypatch.setattr(pc, "subprocess_runner", runner)
    roster.write_text(ROSTER + f"carol,Carol,Lee,carol@example.org,c/x,3,ws-carol,{ALICE},103,\n")
    code = pc.main(["--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"])
    assert code == 1 and not runner.calls
    assert "roster: duplicate ip '203.0.113.5': rows alice and carol" in capsys.readouterr().err
    for dup in (
        "ALICE,A,S,x@example.org,r,9,ws-x,203.0.113.9,109,",  # login, case-insensitively
        "carol,C,L,Alice@Example.org,r,9,ws-x,203.0.113.9,109,",  # e-mail
        "carol,C,L,c@example.org,r,9,ws-alice,203.0.113.9,109,",  # host alias
        "carol,C,L,c@example.org,r,9,ws-x,203.0.113.9,101,",  # Zulip id
    ):
        roster.write_text(ROSTER + dup + "\n")
        assert pc.main(["--roster", str(roster), "--out-dir", str(out_dir), "--tag", "x"]) == 1
        assert "roster: duplicate" in capsys.readouterr().err
    assert not runner.calls


def test_main_dry_run_contacts_nothing(roster, out_dir, monkeypatch, capsys):
    """#361: one line per VM, each command by name and each file sent; `--verbose` prints every
    command whole instead, as before."""
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())
    args = [
        *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
        *("--dry-run", "--only", "alice"),
    ]
    assert pc.main(args) == 0
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith("dry-run")] == [
        f"dry-run alice ({ALICE}): owner check; staging dir; idle-stop hold; scp config.toml "
        "alice/codex.zuliprc alice/claude.zuliprc agent-instructions.md agent_instructions.py "
        "tools_list.py tools-nt-agents-2026-v3.json sage_wrapper.py sage; config installed; "
        "zab installed; service install + doctor; sage wrapper; tools list; agent instructions; "
        "scp warm_cache.py zab-warm-cache.service; warm-up on; staging removed; "
        "hold released"
    ]
    assert "bob" not in out.split("participant")[-1]
    assert pc.main([*args, "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "dry-run: ssh" in out and "dry-run: scp" in out and "dry-run alice" not in out
    assert f"ubuntu@{ALICE}" in out and "ws-alice" not in out.split("dry-run:")[1]


@pytest.mark.parametrize(
    "flags",
    [
        ["--tag", "v0.1.0rc4", "--restart-codex", "--lean-shared", "--lean-reclaim", "apply"],
        ["--tag", "v0.1.0rc4", "--idle-stop", "15", "--adopt-spare", "alice"],
        # #704: the defaults' probes and steps
        "--tag v0.1.0rc4 --lean-shared-default --idle-stop-default 60 --has-waker alice".split(),
        ["--idle-stop", "0", "--lean-reclaim", "report"],
        ["--lean-shared", "--idle-stop", "30"],
        ["--uninstall"],
    ],
)
def test_every_command_a_dry_run_sends_has_a_name(roster, out_dir, monkeypatch, capsys, flags):
    """#361: a name the dry run doesn't know is shown cut (`ssh …`), which a plan would carry
    into its hash: none of the script's own commands is."""
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())
    args = ["--roster", str(roster), "--out-dir", str(out_dir), *flags, "--dry-run"]
    assert pc.main(args) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("dry-run")]
    assert [ln.split(" (")[0] for ln in lines] == ["dry-run alice", "dry-run bob"]
    assert not any("; ssh " in ln or ": ssh " in ln or "local:" in ln for ln in lines), lines


def test_each_remote_command_is_named():
    """The commands no dry run reaches (a spare's adoption, a rollback, the reclaim itself)."""
    named = {
        pc.ADOPT_CMD + shlex.quote("ws-alice"): "owner record",
        ac.remote_chain(pc.as_agent("git", "config", "--global", "user.name", "A S")): (
            "git identity"
        ),
        pc.LEAN_SHARED_UNDO_CMD: "lean rollback",
        pc.lean_reclaim_cmd(True): "lean reclaim apply",
        pc.DOCTOR_CMD: "doctor",
        pc.BRIDGE_START_CMD: "bridge start",
        pc.SERVICE_UNINSTALL_CMD: "service uninstall",
    }
    assert {remote: pc.dry_name(remote) for remote in named} == named
    assert len(set(pc._DRY_NAMES.values())) == len(pc._DRY_NAMES)  # never two alike
    assert pc.dry_name("echo something new") == "ssh echo something new"
    # every fixed command of the script (`*_CMD`), the ones no dry run reaches too
    fixed = {n: getattr(pc, n) for n in dir(pc) if n.endswith("_CMD") and n.isupper()}
    assert {"CODEX_RESTART_CMD", "BRIDGE_STATE_CMD", "OWNER_CMD", "ADOPT_CMD"} <= set(fixed)
    unnamed = [n for n, remote in fixed.items() if pc.dry_name(remote).startswith("ssh ")]
    assert unnamed == []
    assert pc.dry_name(pc.CODEX_RESTART_CMD) == "codex daemon restart"
    assert pc.dry_name(pc.BRIDGE_STATE_CMD) == "bridge state"


def test_as_agent_quotes_and_starts_from_root():
    argv = pc.as_agent("/home/agent/.local/bin/uv", "tool", "install", "a b")
    assert argv[:6] == ["sudo", "-u", "agent", "-H", "bash", "-lc"]
    assert argv[6] == "cd / && /home/agent/.local/bin/uv tool install 'a b'"


def test_run_parallel_keeps_order_and_isolates_exceptions():
    def work(x: int) -> str:
        if x == 2:
            raise RuntimeError("boom")
        return f"ok{x}"

    out = ac.run_parallel([1, 2, 3], work, parallel=3, on_error=lambda x, e: f"err{x}:{e}")
    assert out == ["ok1", "err2:boom", "ok3"]


def test_remote_chain_quotes_every_value():
    assert (
        ac.remote_chain(["echo", "a b"], ["rm", "-rf", "--", "x'y"])
        == "echo 'a b' && rm -rf -- 'x'\"'\"'y'"
    )


def test_select_rows_and_host_of():
    rows = [{"github": "A", "host": "h", "ip": ""}, {"github": "b", "host": "", "ip": "1.2.3.4"}]
    assert [r["github"] for r in ac.select_rows(rows, ["b", "a"])] == ["A", "b"]
    assert ac.select_rows(rows, None) == rows  # the participants: rows with a slug
    with pytest.raises(ValueError, match="not in the roster"):
        ac.select_rows(rows, ["zed"])
    # only the ip column is an address; the host alias resolves through ~/.ssh/config alone
    assert ac.host_of(rows[1]) == "1.2.3.4"
    assert ac.host_of({"github": "c", "ip": " 2001:db8::7 "}) == "2001:db8::7"
    for bad in (
        {"github": "A", "host": "h", "ip": ""},
        {"github": "x", "ip": "ws-x"},
        {"github": "x", "ip": "1.2.3"},
    ):
        with pytest.raises(ValueError, match="no IP address"):
            ac.host_of(bad)


def test_ssh_identity_from_the_environment(monkeypatch):
    """The test VM's own key pair (issue #263): `ZAB_SSH_IDENTITY` for every ssh and scp."""
    monkeypatch.setenv("ZAB_SSH_IDENTITY", "/k/zab-test-vm.pem")
    assert ac.ssh_options("/p/kh")[:4] == ["-i", "/k/zab-test-vm.pem", "-o", "IdentitiesOnly=yes"]
    assert ac.scp_argv("a", "1.2.3.4", "/home/agent/a")[1:3] == ["-i", "/k/zab-test-vm.pem"]
    monkeypatch.setenv("ZAB_SSH_IDENTITY", '/k/"x"')
    with pytest.raises(ValueError):
        ac.ssh_options("/p/kh")


def test_ssh_options_pin_the_host_key_and_forward_nothing(monkeypatch):
    monkeypatch.delenv("ZAB_SSH_IDENTITY", raising=False)
    opts = ac.ssh_options("/p/known_hosts")
    assert opts == [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        'UserKnownHostsFile="/p/known_hosts"',
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
    ]
    assert ac.ssh_argv("1.2.3.4", "x")[-3:] == ["ubuntu@1.2.3.4", "--", "x"]
    assert f'UserKnownHostsFile="{ac.DEFAULT_KNOWN_HOSTS}"' in ac.ssh_argv("1.2.3.4", "x")
    assert ac.DEFAULT_KNOWN_HOSTS.parent.name == "out"  # git-ignored, next to the zuliprcs
    with pytest.raises(ValueError):
        ac.ssh_options('/p/"quoted"')


def test_check_duplicates():
    rows = [
        {"github": "a", "email": "A@x", "host": "ws-a", "ip": "1.1.1.1", "zulip_user_id": "1"},
        {"github": "b", "email": "b@x", "host": "ws-b", "ip": "1.1.1.2", "zulip_user_id": ""},
        {"github": "c", "email": "", "host": "", "ip": "", "zulip_user_id": ""},
    ]
    ac.check_duplicates(rows)  # empty values never collide
    for column, value in (
        ("github", "B"),
        ("email", "a@X"),
        ("host", "WS-A"),
        ("ip", "1.1.1.1"),
        ("zulip_user_id", "1"),
    ):
        with pytest.raises(ValueError, match=f"duplicate {column}"):
            ac.check_duplicates([*rows, {**rows[2], "github": "d", column: value}])


def test_redact_covers_zulip_keys_and_tokens():
    assert ac.redact("key=" + "k" * 32 + " ghp_" + "a" * 36) == "key=[redacted] [redacted]"
    assert ac.first_line("\n  sk-ant-" + "x" * 30 + " leaked\n") == "[redacted] leaked"


@pytest.mark.parametrize(
    "splitter", ["\u200b", "\xad", "\ufeff", "\u202e", "\x00", "\x1b", "\ufe0f", "\x7f"]
)
@pytest.mark.parametrize("limit", [160, 300, 80])
def test_first_line_strips_redacts_then_cuts(splitter, limit):
    """The review of #380: status.py cuts `doctor_failures` at 160, `last_verdict` at 300 and
    `last_error` at 80. A token that an invisible character splits across the cut kept 38 of
    its 40 characters, too few for its pattern; stripped first, it is whole and redacted."""
    token, lead = "ghp_" + "Z" * 36, limit - 20  # the cut falls inside the token
    line = "x" * lead + token[:6] + splitter + token[6:] + " tail"
    cell = ac.first_line(f"\n{splitter}\n{line}\n", limit)  # an invisible line is empty
    assert cell == "x" * lead + "[redacted] tail", cell
    assert ac.redact("key=" + "k" * 16 + splitter + "k" * 16) == "key=[redacted]"
    assert ac.strip_invisible("a\u200bé\u2028b\n\tc\U000e0100") == "aéb\n\tc"


def test_the_strip_and_the_patterns_are_the_admin_bots():
    """The review of #380: the same invisible characters as the admin bots' scrubber (copied:
    these scripts import nothing from zab), and its JWT and AWS secret rules, since VM text
    reaches status.py's cells: `auth.json<TAB>eyJ…` came out whole."""
    from zab.admin.scrub import _INVISIBLE_RANGES

    assert ac._INVISIBLE_RANGES == _INVISIBLE_RANGES
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"
    assert ac.first_line(f"codex:login: auth.json\t{jwt} tail") == (
        "codex:login: auth.json\t[redacted] tail"
    )
    assert ac.redact(f"token\t{jwt}") == "token\t[redacted]"
    assert ac.redact("verdict: token=" + "T" * 24) == "verdict: token=[redacted]"
    assert ac.redact("aws_secret_access_key = " + "w" * 40) == "aws_secret_access_key = [redacted]"
    assert ac.redact("tokens: 12345678901234567890 and max_tokens=12") == (
        "tokens: 12345678901234567890 and max_tokens=12"  # not a token's value
    )


def test_the_copied_rules_are_the_admin_bots_word_for_word():
    """#347 (the approval of #380): `_SECRET_RES` copies the JWT rule of `zab.render.redact`
    and the AWS secret rules of `zab.admin.scrub`, since these scripts import nothing from
    zab; the examples above test them, this keeps them from drifting. The bots' named group
    `prefix` is a plain group here: `redact` keeps group 1."""
    from zab.admin import scrub
    from zab.render import redact as bots

    copied = {(p.pattern, p.flags) for p in ac._SECRET_RES}
    (jwt,) = [p for p, label in bots.SECRET_PATTERNS if label == "[redacted jwt]"]
    assert (jwt.pattern, jwt.flags) in copied
    for rule in (scrub._AWS_SECRET, scrub._AWS_TOKEN):
        assert (rule.pattern.replace("(?P<prefix>", "(", 1), rule.flags) in copied, rule.pattern


def test_load_zuliprc_requires_https(tmp_path):
    rc = tmp_path / "rc"
    rc.write_text("[api]\nemail=e\nkey=k\nsite=http://insecure\n")
    with pytest.raises(ValueError):
        ac.load_zuliprc(str(rc))
    rc.write_text("[api]\nemail=e\nkey=k\nsite=https://ok/\n")
    assert ac.load_zuliprc(str(rc)) == ("https://ok", "e", "k")


def test_format_table_aligns():
    text = ac.format_table(["a", "bb"], [["1", "2"], ["333", "4"]])
    assert text.splitlines()[0] == "a    bb" and text.splitlines()[2] == "1    2 "
    assert isinstance(pathlib.Path(pc.OUT_DIR), pathlib.Path)


def test_fake_runner_interface_matches_protocol():
    def check(r: ac.Runner) -> None:
        r(["true"])

    check(FakeRunner())
    res: Mapping[str, str] = {}
    assert res == {}


# ------------------------------------------------------------ no GitHub (issue #36, Sun Oct 4)


def test_render_config_allows_the_roster_email_and_names_everything_after_the_slug(tmp_path):
    """A's settled key `[[bots]] allow_emails`: the roster e-mail alone admits the participant,
    so one who joins Zulip after the push needs no second push; the id is added once known."""
    row = {"slug": "ada-l", "github": "", "email": "ada@example.org", "zulip_user_id": ""}
    data = tomllib.loads(pc.render_config_toml(row))
    assert data["bridge"]["machine"] == "ada-l"
    assert data["bridge"]["default_project"] == "~/work/ada-l"
    for bot in data["bots"]:
        assert bot["allow_emails"] == ["ada@example.org"] and "allow_user_ids" not in bot
    both = tomllib.loads(pc.render_config_toml({**row, "zulip_user_id": "101"}))
    assert all(b["allow_user_ids"] == [101] for b in both["bots"])
    assert all(b["allow_emails"] == ["ada@example.org"] for b in both["bots"])
    with pytest.raises(ValueError, match="no e-mail"):
        pc.render_config_toml({**row, "email": ""})
    # the bridge's own loader accepts both forms (codex round 1 on #97: needs #92's key)
    from zab.config import load_config

    for variant in (row, {**row, "zulip_user_id": "101"}):
        path = tmp_path / f"config-{variant['zulip_user_id'] or 'email'}.toml"
        path.write_text(pc.render_config_toml(variant))
        cfg = load_config(path)
        assert all(b.allow_emails == ["ada@example.org"] for b in cfg.bots)
        assert cfg.bridge.machine == "ada-l"


def test_legacy_owner_records_name_the_login():
    """VMs first-booted before the slug (rc3) record only the login; it still matches."""
    stdout = (
        f"{pc.OWNER_MARK}JSON\n" + json.dumps({"github": "Alice", "repo": "x"}) + "\n"
        f"{pc.OWNER_MARK}HOST\nws-Alice\n{pc.OWNER_MARK}END\n"
    )
    owner = pc.parse_owner(stdout)
    assert owner.slug == "alice"
    assert pc.owner_problem(owner, "alice", adopt=False, github="Alice") is None


def test_the_release_must_be_named(roster, out_dir, capsys):
    """Codex round 3 on #97: a default (rc1) would install a zab that rejects `allow_emails`."""
    with pytest.raises(SystemExit) as exc:
        pc.main(["--roster", str(roster), "--out-dir", str(out_dir)])
    assert exc.value.code == 2 and "--tag" in capsys.readouterr().err
    assert (
        pc.main(["--roster", str(roster), "--out-dir", str(out_dir), "--uninstall", "--dry-run"])
        == 0
    )  # uninstalling needs no release


def test_rollout_refuses_a_vm_that_trusts_a_previous_github_login():
    """Codex round 7 on #97: a corrected or removed login leaves the old account's keys on the
    VM; the bots' zuliprc files must not be copied there until the record says otherwise."""

    def owner(record, host="ws-alice"):
        return pc.parse_owner(
            f"{pc.OWNER_MARK}JSON\n{json.dumps(record)}\n"
            f"{pc.OWNER_MARK}HOST\n{host}\n{pc.OWNER_MARK}END\n"
        )

    vm = owner({"slug": "alice", "github": "alcie", "repo": "alice"})
    problem = pc.owner_problem(vm, "alice", adopt=False, github="alice")
    assert problem and "records GitHub login 'alcie'" in problem
    assert "ssh-import-id gh:alice" in problem
    problem = pc.owner_problem(vm, "alice", adopt=False, github="")
    assert problem and "no GitHub login in the roster" in problem
    assert pc.owner_problem(vm, "alice", adopt=False, github="ALCIE") is None  # same login
    plain = owner({"slug": "alice", "github": "", "repo": "alice"})
    assert pc.owner_problem(plain, "alice", adopt=False, github="") is None
    legacy = owner({"github": "Alice", "repo": "x"}, host="ws-Alice")  # rc3 record
    assert pc.owner_problem(legacy, "alice", adopt=False, github="Alice") is None


class DaemonLate(FakeRunner):
    """The Codex daemon answers only from the `ready_after`-th doctor on (rc3 rollout)."""

    def __init__(self, ready_after: int, **kw):
        super().__init__(**kw)
        self.ready_after = ready_after
        self.doctors = 0

    def __call__(self, argv, **kw):
        if argv[0] == "ssh" and argv[-1] in (pc.SERVICE_INSTALL_CMD, pc.DOCTOR_CMD):
            self.calls.append(list(argv))
            self.doctors += 1
            ok = self.doctors >= self.ready_after
            check = {"name": "codex:connection", "ok": ok}
            if not ok:
                check["detail"] = "daemon unavailable"
            return ac.RunResult(0, json.dumps([{"name": "config", "ok": True}, check]) + "\n", "")
        return super().__call__(argv, **kw)


def test_rollout_waits_for_the_codex_daemon(out_dir, monkeypatch):
    slept = []
    monkeypatch.setattr(pc.time, "sleep", slept.append)
    runner = DaemonLate(ready_after=3)
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert result.doctor_ok and not result.failures
    assert runner.doctors == 3 and slept == [pc.DAEMON_POLL_S] * 2
    assert sum(1 for c in runner.calls if c[-1] == pc.DOCTOR_CMD) == 2


def test_rollout_reports_a_daemon_that_never_comes_up(out_dir, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pc.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    runner = DaemonLate(ready_after=99)
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), runner)
    assert result.doctor_ok is False
    assert result.failures == ["doctor: codex:connection: daemon unavailable"]
    assert runner.doctors == 1 + int(pc.DAEMON_WAIT_S // pc.DAEMON_POLL_S)  # about 60 s


def test_a_limit_that_waits_for_the_next_start_is_a_note(out_dir):
    """#664, review of #665: `zab service install`'s line for a Codex daemon that `prlimit`
    couldn't raise reaches the row's notes, word for word, as the installed release prints it;
    a line that only looks like it doesn't. Not a failure."""
    from zab.commands import service

    note = service.limit_note("codex-app-server.service", 65536)
    assert pc.LIMIT_NOTE_RE.fullmatch(note)
    assert pc.LIMIT_NOTE_RE.fullmatch(service.limit_note("codex-app-server-1.service", 4096))
    forged = f"{note}; and more\n codex-app-server.service: x\n{note}`\n"
    doctor = '[{"name": "config", "ok": true}]\n'

    class Limited(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[0] == "ssh" and argv[-1] == pc.SERVICE_INSTALL_CMD:
                self.calls.append(list(argv))
                return ac.RunResult(0, f"Installed\n{forged}{note}\n{doctor}", "")
            return super().__call__(argv, **kw)

    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(tag="v0.1.0", out_dir=out_dir), Limited())
    assert result.ok and result.notes == [note]
    assert f"note: {note}" in pc.render([result])


def test_rollout_does_not_wait_on_other_failures(out_dir, monkeypatch):
    monkeypatch.setattr(pc.time, "sleep", lambda s: pytest.fail("no retry for a sign-in"))

    class SignedOut(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[0] == "ssh" and argv[-1] == pc.SERVICE_INSTALL_CMD:
                self.calls.append(list(argv))
                body = [{"name": "claude account main", "ok": False, "detail": "not signed in"}]
                return ac.RunResult(0, json.dumps(body) + "\n", "")
            return super().__call__(argv, **kw)

    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), SignedOut())
    # no wait, and (PM on #36, Mon 07:35) a missing sign-in alone is no failure either
    assert result.ok and result.sign_in_pending and not result.failures


def test_a_failing_row_is_not_marked_sign_in_pending(out_dir, monkeypatch):
    """PM on #36 (Mon 13:45): `sign_in_pending` is set for an otherwise passing doctor only."""
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)

    class Mixed(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[0] == "ssh" and argv[-1] == pc.SERVICE_INSTALL_CMD:
                self.calls.append(list(argv))
                body = [
                    {"name": "claude account main", "ok": False, "detail": "not signed in"},
                    {"name": "zulip:claude", "ok": False, "detail": "401"},
                ]
                return ac.RunResult(0, json.dumps(body) + "\n", "")
            return super().__call__(argv, **kw)

    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(out_dir=out_dir), Mixed())
    assert result.doctor_ok is False and not result.sign_in_pending and not result.ok


def test_keys_in_the_legacy_login_folder_are_found_and_reported(out_dir):
    """Drew's rc4 push (issue #36): rc3 wrote `out/<github>/` with the login's capitals."""
    (out_dir / "alice").rename(out_dir / "Alice")
    row = {"github": "Alice", "slug": "alice", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(
        row, pc.Options(tag="v0.1.0rc1", out_dir=out_dir, known_hosts=KH), FakeRunner()
    )
    assert result.ok and not result.failures
    assert result.notes == [
        "keys from the legacy out/Alice/ (zulip_provision.py moves them to out/alice/)"
    ]
    assert "note: keys from the legacy out/Alice/" in pc.render([result])
    (out_dir / "Alice").rename(out_dir / "elsewhere")  # neither folder: still the plain failure
    result = pc.rollout_vm(
        row, pc.Options(tag="v0.1.0rc1", out_dir=out_dir, known_hosts=KH), FakeRunner()
    )
    assert "missing codex zuliprc: run zulip_provision.py first" in result.failures


@pytest.mark.parametrize(
    ("folders", "row", "expect"),
    [
        (["alice"], {"github": "Alice", "slug": "alice"}, ("alice", False)),  # slug wins
        (["Alice", "alice"], {"github": "Alice", "slug": "alice"}, ("alice", False)),
        (["Alice"], {"github": "Alice", "slug": "alice"}, ("Alice", True)),
        # codex review on #114: a login that is not the slug in another case is never taken,
        # since `out/<it>/` may be another participant's keys (`bob` is Bob's slug)
        (["Alice"], {"github": "Alice", "slug": "al"}, ("al", False)),
        (["bob"], {"github": "bob", "slug": "alice"}, ("alice", False)),
        (["Bob"], {"github": "Bob", "slug": "alice"}, ("alice", False)),
        ([], {"github": "Alice", "slug": "alice"}, ("alice", False)),
        (["x"], {"github": "../x", "slug": "alice"}, ("alice", False)),  # not a login
        ([], {"github": "", "slug": "alice"}, ("alice", False)),
    ],
)
def test_keys_dir(tmp_path, folders, row, expect):
    for name in folders:
        (tmp_path / name).mkdir()
    path, note = ac.keys_dir(tmp_path, row)
    assert (path.name, bool(note)) == expect


def test_the_config_names_the_central_file_server(tmp_path):
    """SPEC §10.7 (Drew on #36, Mon 04:01): push_config renders [render] refserve_url, the
    central file server; the VM's zab reads it, so it parses as a real zab config would."""
    import tomllib

    row = {"slug": "alice", "email": "alice@example.org", "repo": ""}
    data = tomllib.loads(pc.render_config_toml(row))
    assert data["render"] == {
        "refserve_url": "https://files.mathagents.org",
        "refserve_owner": "MathAgents",  # written, never left to the bridge's default (#693)
    }
    assert "render" not in tomllib.loads(pc.render_config_toml(row, refserve_url=""))
    path = tmp_path / "config.toml"
    path.write_text(
        pc.render_config_toml(
            row, refserve_url="https://files.example.org", refserve_owner="Example-Org"
        )
    )
    from zab.config import load_config

    config = load_config(path)
    assert config.render.refserve_url == "https://files.example.org"
    assert config.render.refserve_owner == "Example-Org"
    with pytest.raises(ValueError, match="not a GitHub owner"):
        pc.render_config_toml(row, refserve_owner='x"\n[bridge]')


def test_main_checks_the_refserve_url(capsys):
    with pytest.raises(SystemExit):
        pc.main(["--roster", "r.csv", "--wheel", "w.whl", "--refserve-url", "http://x"])
    assert "https://" in capsys.readouterr().err


def test_another_organizations_workshop_gets_its_snapshots(tmp_path, out_dir):
    """#693 criterion 2: the config a rollout sends names the workshop's organization as the
    file server's owner, which the VM's bridge then publishes snapshots for
    (tests/workspace/test_publish_snapshot.py, with a config like this one)."""

    class Reading(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[dict] = []

        def __call__(self, argv, **kw):
            staged = [w for w in argv if w.endswith("/config.toml") and os.path.isfile(w)]
            self.sent += [tomllib.loads(pathlib.Path(w).read_text()) for w in staged]
            return super().__call__(argv, **kw)

    runner = Reading()
    opts = pc.Options(
        tag="v0.2.0",
        out_dir=out_dir,
        known_hosts=KH,
        refserve_url="https://files.example.org",
        refserve_owner="Example-Org",
    )
    assert pc.rollout_vm(ALICE_ROW, opts, runner).ok
    [sent] = runner.sent
    assert sent["render"] == {
        "refserve_url": "https://files.example.org",
        "refserve_owner": "Example-Org",
    }


@pytest.mark.parametrize(
    "flags,error",
    [
        (["--wheel", "w.whl", "--refserve-owner", "a/b"], "must be a GitHub organization"),
        (["--wheel", "w.whl", "--refserve-owner=-x"], "must be a GitHub organization"),
        (["--wheel", "w.whl", "--refserve-owner", "x" * 40], "must be a GitHub organization"),
        (["--idle-stop", "15", "--refserve-owner", "Example-Org"], "needs a release"),
        (["--uninstall", "--refserve-owner", "Example-Org"], "needs a release"),
    ],
)
def test_main_refuses_a_refserve_owner_it_cant_write(capsys, flags, error):
    """#693: a name GitHub can't have, or a run that writes no config.toml: refused, never
    dropped without a word."""
    with pytest.raises(SystemExit):
        pc.main(["--roster", "r.csv", *flags])
    assert error in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags,owner", [([], "MathAgents"), (["--refserve-owner", "Example-Org"], "Example-Org")]
)
def test_main_takes_the_workshops_organization(tmp_path, monkeypatch, flags, owner):
    """A hand run takes it as a flag; its default is the default --refserve-url's, the
    MathAgents workshops' (the admin README's table of defaults)."""
    captured = {}
    real = pc.Options
    monkeypatch.setattr(pc, "artifact_config", REAL_ARTIFACT_CONFIG)

    def options(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(pc, "Options", options)
    monkeypatch.setattr(pc, "load_roster", lambda path: [])
    wheel = fake_wheel(tmp_path, CONFIG_WITH)
    assert pc.main(["--roster", "r.csv", "--wheel", wheel, *flags, "--dry-run"]) == 0
    assert captured["refserve_owner"] == owner


SIGNED_OUT = [
    {"name": "config", "ok": True},
    {"name": "codex:connection", "ok": True, "detail": "/home/agent/.codex"},
    {"name": "codex:login", "ok": False, "detail": ""},
    {
        "name": "claude account main",
        "ok": False,
        "detail": "not signed in: send !login to this bot in a direct message",
    },
]


class DoctorSays(FakeRunner):
    """The service install's doctor answers `checks` (a JSON list)."""

    def __init__(self, checks, **kw):
        super().__init__(**kw)
        self.checks = checks

    def __call__(self, argv, **kw):
        if argv[0] == "ssh" and argv[-1] in (pc.SERVICE_INSTALL_CMD, pc.DOCTOR_CMD):
            self.calls.append(list(argv))
            return ac.RunResult(0, json.dumps(self.checks) + "\n", "")
        return super().__call__(argv, **kw)


def test_only_sign_in_checks_failing_is_ok_sign_in_pending(roster, out_dir, monkeypatch, capsys):
    """PM on #36 (Mon 07:35): on Wednesday nobody can sign in yet, so a VM whose only failing
    doctor checks are the agents' sign-ins is `ok (sign-in pending)` and no failure."""
    monkeypatch.setattr(pc, "subprocess_runner", DoctorSays(SIGNED_OUT))
    code = pc.main(
        [
            *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
            *("--known-hosts", KH),
        ]
    )
    out = capsys.readouterr()
    assert code == 0 and "failed" not in out.err
    rows = [line for line in out.out.splitlines() if line.startswith(("alice", "bob"))]
    assert len(rows) == 2 and all("ok (sign-in pending)" in line for line in rows)
    assert "FAIL" not in out.out
    assert "2 VM(s) ok, sign-in pending" in out.out


def test_a_sign_in_check_plus_another_failure_is_a_failure(roster, out_dir, monkeypatch, capsys):
    broken = [*SIGNED_OUT, {"name": "alice:allowlist", "ok": False, "detail": "no user"}]
    monkeypatch.setattr(pc, "subprocess_runner", DoctorSays(broken))
    code = pc.main(
        [
            *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
            *("--known-hosts", KH),
        ]
    )
    out = capsys.readouterr()
    assert code == 1 and "2 VM(s) failed" in out.err
    assert "sign-in pending" not in out.out
    rows = [line for line in out.out.splitlines() if line.startswith(("alice", "bob"))]
    assert all(" FAIL " in line and "alice:allowlist" in line for line in rows)


#: The doctor's allowlist checks on a VM whose participant hasn't joined Zulip (#401): the
#: detail from #401 on, and the empty one of a doctor before it (0.2.3).
WAITING = (
    "no allowed user yet: no Zulip user with the allowed e-mail visible to this bot "
    "(not joined yet, or the address is hidden from members)"
)
UNJOINED = [{"name": f"{b}:allowlist", "ok": False, "detail": WAITING} for b in ("codex", "claude")]
UNJOINED_023 = [{"name": f"{b}:allowlist", "ok": False, "detail": ""} for b in ("codex", "claude")]
NOBODY = "nobody allowed: no allow_user_ids, no allow_emails and no bot owner"


def push(roster, out_dir, monkeypatch, capsys, checks, rows, runner=None):
    """`push_config.py` over `rows` (the roster's, `zulip_user_id` and `email` replaced) with
    every doctor answering `checks` (or `runner`): (exit code, stdout, stderr)."""
    lines = ROSTER.splitlines()
    for i, (uid, email) in enumerate(rows, start=1):
        cells = lines[i].split(",")
        cells[3], cells[8] = email, uid
        lines[i] = ",".join(cells)
    roster.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(pc, "subprocess_runner", runner or DoctorSays(checks))
    code = pc.main(
        [
            *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
            *("--known-hosts", KH),
        ]
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def table_rows(out: str) -> dict[str, str]:
    return {line.split()[0]: line for line in out.splitlines() if line.startswith(("alice", "bob"))}


@pytest.mark.parametrize("unjoined", [UNJOINED, UNJOINED_023], ids=["0.2.4", "0.2.3"])
def test_an_unbound_row_is_owner_and_sign_in_pending(
    roster, out_dir, monkeypatch, capsys, unjoined
):
    """#401: alice's row has her e-mail and no Zulip ID yet (she hasn't joined), so her bots'
    allowlist checks fail; that is pending, not a failure, with the doctor's detail (0.2.4) or
    without it (0.2.3). Bob's bound row, whose doctor fails the same way, stays a failure."""
    code, out, err = push(
        roster,
        out_dir,
        monkeypatch,
        capsys,
        [*SIGNED_OUT, *unjoined],
        [("", "alice@example.org"), ("102", "bob@example.org")],
    )
    rows = table_rows(out)
    assert "ok (owner+sign-in pending)" in rows["alice"] and "FAIL" not in rows["alice"]
    assert " FAIL " in rows["bob"] and "doctor: codex:allowlist" in rows["bob"]
    assert "doctor: codex:login" in rows["bob"]  # listed with the real failure, as before
    assert code == 1 and "1 VM(s) failed" in err
    assert "1 VM(s) ok, owner pending: expected until the participants join Zulip" in out
    code, out, err = push(
        roster,
        out_dir,
        monkeypatch,
        capsys,
        [*SIGNED_OUT, *unjoined],
        [("", "alice@example.org"), ("", "bob@example.org")],
    )
    assert code == 0 and "failed" not in err
    rows = table_rows(out)
    assert all("ok (owner+sign-in pending)" in line for line in rows.values()), rows
    assert (
        "\n2 VM(s) ok, sign-in pending: expected until the participants send !login to their "
        "bots\n2 VM(s) ok, owner pending: expected until the participants join Zulip and "
        "!provision zulip (zulip_provision.py) records their IDs\n"
    ) in out


def test_an_unbound_row_signed_in_already_is_owner_pending(roster, out_dir, monkeypatch, capsys):
    code, out, _ = push(
        roster,
        out_dir,
        monkeypatch,
        capsys,
        [{"name": "config", "ok": True}, *UNJOINED],
        [("", "alice@example.org"), ("", "bob@example.org")],
    )
    rows = table_rows(out)
    assert code == 0 and all("ok (owner pending)" in line for line in rows.values()), rows
    assert "sign-in pending" not in out and "\n2 VM(s) ok, owner pending" in out


def test_an_unbound_row_with_another_allowlist_failure_fails(roster, out_dir, monkeypatch, capsys):
    """#401: only the doctor's waiting detail (or 0.2.3's empty one) is pending; nobody allowed
    at all, or any other detail, stays a failure on an unbound row too; so does any other
    failing check beside a pending allowlist."""
    for broken in (
        [*SIGNED_OUT, {"name": "codex:allowlist", "ok": False, "detail": NOBODY}],
        [*SIGNED_OUT, {"name": "codex:allowlist", "ok": False, "detail": "no user"}],
        [*SIGNED_OUT, *UNJOINED, {"name": "zulip:claude", "ok": False, "detail": "401"}],
    ):
        code, out, _ = push(
            roster,
            out_dir,
            monkeypatch,
            capsys,
            broken,
            [("", "alice@example.org"), ("", "bob@example.org")],
        )
        rows = table_rows(out)
        assert code == 1 and all(" FAIL " in line for line in rows.values()), rows
        assert "pending" not in out
    # the failures column lists the real failures, not the pending allowlist checks
    assert all("allowlist" not in line and "zulip:claude: 401" in line for line in rows.values())


class DoctorByHost(DoctorSays):
    """Each VM's doctor answers its own checks: `by_ip` maps the row's IP to them; the service
    step of the VMs in `service_failed` also prints `SERVICE_FAILED_MARK`."""

    def __init__(self, by_ip, service_failed=(), **kw):
        super().__init__([], **kw)
        self.by_ip, self.service_failed = by_ip, service_failed

    def __call__(self, argv, **kw):
        if argv[0] == "ssh" and argv[-1] in (pc.SERVICE_INSTALL_CMD, pc.DOCTOR_CMD):
            ip = next(ip for ip in self.by_ip if any(ip in a for a in argv))
            self.checks = self.by_ip[ip]
            res = super().__call__(argv, **kw)
            if ip in self.service_failed and argv[-1] == pc.SERVICE_INSTALL_CMD:
                return ac.RunResult(0, f"{pc.SERVICE_FAILED_MARK}\n{res.stdout}", "")
            return res
        return super().__call__(argv, **kw)


@pytest.mark.parametrize("why", ["doctor", "service"])
def test_the_owner_pending_line_counts_the_ok_rows_only(roster, out_dir, monkeypatch, capsys, why):
    """#401 (reviewer 6205530228 on #402): two unbound rows whose allowlists both wait; bob's
    also fails, by another doctor check or by the service step (a failure that leaves his
    doctor owner-pending). Alice is `ok (owner+sign-in pending)`, and the lines under the
    table count her alone."""
    pending = [*SIGNED_OUT, *UNJOINED]
    if why == "doctor":
        bob = [*pending, {"name": "zulip:claude", "ok": False, "detail": "401"}]
        runner = DoctorByHost({ALICE: pending, BOB: bob})
    else:
        runner = DoctorByHost({ALICE: pending, BOB: pending}, service_failed={BOB})
    code, out, err = push(
        roster,
        out_dir,
        monkeypatch,
        capsys,
        [],
        [("", "alice@example.org"), ("", "bob@example.org")],
        runner=runner,
    )
    rows = table_rows(out)
    assert "ok (owner+sign-in pending)" in rows["alice"]
    assert (" FAIL " if why == "doctor" else "service install / restart: failed") in rows["bob"]
    assert code == 1 and "1 VM(s) failed" in err
    assert "\n1 VM(s) ok, sign-in pending: " in out and "\n1 VM(s) ok, owner pending: " in out


def test_a_row_with_neither_e_mail_nor_id_fails(roster, out_dir, monkeypatch, capsys):
    """#401: a row with nothing to allow never gets as far as the doctor."""
    code, out, _ = push(
        roster,
        out_dir,
        monkeypatch,
        capsys,
        SIGNED_OUT,
        [("", ""), ("102", "bob@example.org")],
    )
    rows = table_rows(out)
    assert code == 1 and " FAIL " in rows["alice"] and "no e-mail in the roster" in rows["alice"]
    assert "ok (sign-in pending)" in rows["bob"]


# ------------------------------------------------------------- a hand-over's checkout (#458)

#: Alice's checkout (her row's `repo` is alice/thesis) as the doctor on her VM names it.
ALICE_CHECKOUT = "/home/agent/work/thesis"
#: The doctor's check of it before provision_repos.py has cloned it: a spare's first boot makes
#: only ~/work (`aws_common.SPARE_USER_DATA`), and the doctor fails a path that isn't a git
#: repository with this detail (`zab.commands.doctor.checks`).
UNCLONED = {"name": f"project:{ALICE_CHECKOUT}", "ok": False, "detail": "git repository"}
HANDOVER_ROW = {
    "github": "alice", "repo": "alice/thesis", "zulip_user_id": "101", "ip": ALICE,
    "email": "alice@example.org", "first_name": "Alice", "last_name": "Smith",
}  # fmt: skip


class ProbeSays(DoctorSays):
    """The doctor answers `checks`; the checkout probe exits `probe` (0: nothing there)."""

    def __init__(self, checks, probe=0, **kw):
        super().__init__(checks, **kw)
        self.probe = probe

    def __call__(self, argv, **kw):
        if argv[0] == "ssh" and argv[-1] == pc.checkout_absent_cmd(ALICE_CHECKOUT):
            self.calls.append(list(argv))
            return ac.RunResult(self.probe, "", "")
        return super().__call__(argv, **kw)


def handover(out_dir, checks, *, adopt=("alice",), spare=True, probe=0):
    """rollout_vm on alice's VM (still the spare, or already hers) with `--adopt-spare` for
    `adopt`: (result, runner)."""
    record = owner_stdout("", spare="zab-spare-1") if spare else owner_stdout("alice")
    runner = ProbeSays(checks, probe, owners={ALICE: record})
    opts = pc.Options(out_dir=out_dir, adopt_spares=frozenset(adopt))
    return pc.rollout_vm(HANDOVER_ROW, opts, runner), runner


def probed(runner) -> bool:
    return pc.checkout_absent_cmd(ALICE_CHECKOUT) in runner.remotes(ALICE)


def test_the_checkout_check_is_the_doctors_name_for_the_default_project(tmp_path, monkeypatch):
    """The name push_config matches is `project:` and the config's default_project as zab
    resolves it for agent (HOME /home/agent), so the rule can't drift from the profile."""
    from zab.config import load_config

    monkeypatch.setenv("HOME", pc.AGENT_HOME)
    for row, name in (
        (HANDOVER_ROW, f"project:{ALICE_CHECKOUT}"),
        (
            {"slug": "dave", "email": "dave@example.org", "repo": ""},
            "project:/home/agent/work/dave",
        ),
    ):
        path = tmp_path / "config.toml"
        path.write_text(pc.render_config_toml(row))
        assert pc.checkout_check(row) == name
        assert f"project:{load_config(path).bridge.default_project}" == name
    # nothing at all, not even a dangling link; as root; the path quoted
    p = "/home/agent/work/a b"
    probe = shlex.split(pc.checkout_absent_cmd(p))
    assert probe == ["sudo", "test", "!", "-e", p, "&&", "sudo", "test", "!", "-L", p]
    assert pc.dry_name(pc.checkout_absent_cmd(ALICE_CHECKOUT)) == "checkout probe"


def test_a_hand_over_counts_its_checkout_not_cloned_yet_as_pending(out_dir):
    """#458: `!adopt-spare`'s first push_config.py runs before provision_repos.py clones the
    checkout. With --adopt-spare, the doctor's check of exactly that path is pending once the
    probe finds nothing there; the sign-ins stay pending as before."""
    result, runner = handover(out_dir, [*SIGNED_OUT, UNCLONED])
    assert result.ok and result.adopted == "zab-spare-1", result.failures
    assert result.project_pending and result.sign_in_pending and not result.owner_pending
    assert probed(runner)
    assert pc._doctor_cell(result) == "ok (project+sign-in pending)"
    # the same step again, once the record names her (a deployer finishing the chain by hand)
    result, runner = handover(out_dir, [*SIGNED_OUT, UNCLONED], spare=False)
    assert result.ok and result.project_pending and not result.adopted and probed(runner)
    # an unbound row: the allowlists wait too
    unbound = {**HANDOVER_ROW, "zulip_user_id": ""}
    runner = ProbeSays([*SIGNED_OUT, *UNJOINED, UNCLONED], owners={ALICE: owner_stdout("alice")})
    result = pc.rollout_vm(
        unbound, pc.Options(out_dir=out_dir, adopt_spares=frozenset({"alice"})), runner
    )
    assert result.ok and pc._doctor_cell(result) == "ok (project+owner+sign-in pending)"
    # a checkout that is there: no probe, nothing pending
    result, runner = handover(out_dir, SIGNED_OUT)
    assert result.ok and not result.project_pending and not probed(runner)


def test_main_reports_the_pending_checkout_under_the_table(roster, out_dir, monkeypatch, capsys):
    """#458: exit 0, and a line under the table says what the checkout waits for."""
    runner = DoctorByHost(
        {ALICE: [*SIGNED_OUT, UNCLONED], BOB: SIGNED_OUT},
        owners={ALICE: owner_stdout("", spare="zab-spare-1")},
    )
    monkeypatch.setattr(pc, "subprocess_runner", runner)
    args = [
        *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
        *("--known-hosts", KH, "--adopt-spare", "alice"),
    ]
    assert pc.main(args) == 0
    out = capsys.readouterr()
    rows = table_rows(out.out)
    assert "ok (project+sign-in pending)" in rows["alice"] and "was zab-spare-1" in rows["alice"]
    assert "ok (sign-in pending)" in rows["bob"] and "failed" not in out.err
    assert (
        "\n2 VM(s) ok, sign-in pending: expected until the participants send !login to their "
        "bots\n1 VM(s) ok, project pending: expected until provision_repos.py, next in the "
        "hand-over (ORGANIZERS §7.2), clones the checkout; the push_config.py after it checks it\n"
    ) in out.out


@pytest.mark.parametrize(
    ("checks", "kw", "listed", "probe_runs"),
    [
        # another failing check beside the pending checkout
        (
            [*SIGNED_OUT, UNCLONED, {"name": "codex:permissions", "ok": False, "detail": "0644"}],
            {},
            "doctor: codex:permissions: 0644",
            True,
        ),
        # a project check of another path
        (
            [*SIGNED_OUT, {**UNCLONED, "name": "project:/home/agent/work/other"}],
            {},
            "doctor: project:/home/agent/work/other: git repository",
            False,
        ),
        # something is at the path that isn't a git repository, or the probe can't tell
        (
            [*SIGNED_OUT, UNCLONED],
            {"probe": 1},
            f"doctor: project:{ALICE_CHECKOUT}: git repository",
            True,
        ),
        (
            [*SIGNED_OUT, UNCLONED],
            {"probe": 255},
            f"doctor: project:{ALICE_CHECKOUT}: git repository",
            True,
        ),
        # --adopt-spare for another row loosens nothing on this one
        (
            [*SIGNED_OUT, UNCLONED],
            {"adopt": ("bob",), "spare": False},
            f"doctor: project:{ALICE_CHECKOUT}: git repository",
            False,
        ),
    ],
    ids=["other-check", "other-project", "not-a-repo", "probe-failed", "other-row"],
)
def test_a_hand_over_fails_on_any_other_doctor_failure(out_dir, checks, kw, listed, probe_runs):
    """#458: only the row's own checkout, absent, is pending; every other failure fails."""
    result, runner = handover(out_dir, checks, **kw)
    assert not result.ok and result.doctor_ok is False and not result.project_pending
    assert listed in result.failures
    assert probed(runner) is probe_runs
    assert pc._doctor_cell(result) == "FAIL"


def test_the_last_step_fails_while_the_checkout_is_still_missing(
    roster, out_dir, monkeypatch, capsys
):
    """#458: the push_config.py after provision_repos.py, without --adopt-spare, checks the
    doctor in full: a checkout still missing (provision_repos.py skipped the row, or cloned
    elsewhere) fails it, and so the chain; once it is there, the row is `ok (sign-in pending)`."""
    result, runner = handover(out_dir, [*SIGNED_OUT, UNCLONED], adopt=(), spare=False)
    assert not result.ok and result.doctor_ok is False and not result.project_pending
    assert f"doctor: project:{ALICE_CHECKOUT}: git repository" in result.failures
    assert not probed(runner)
    result, _ = handover(out_dir, SIGNED_OUT, adopt=(), spare=False)
    assert result.ok and result.sign_in_pending and not result.project_pending
    monkeypatch.setattr(pc, "subprocess_runner", DoctorByHost({ALICE: [*SIGNED_OUT, UNCLONED]}))
    args = [
        *("--roster", str(roster), "--out-dir", str(out_dir), "--tag", "v0.1.0rc4"),
        *("--known-hosts", KH, "--only", "alice"),
    ]
    assert pc.main(args) == 1
    out = capsys.readouterr()
    assert " FAIL " in table_rows(out.out)["alice"] and "project pending" not in out.out
    assert "1 VM(s) failed" in out.err


def test_the_allowlist_rule(monkeypatch):
    """#401 (`admin_common.allowlist_pending`): by the check's name and detail, and the row."""
    from zab.commands.doctor import ALLOWLIST_PENDING

    assert ac.ALLOWLIST_PENDING == ALLOWLIST_PENDING  # a copy: the scripts import nothing from zab
    unbound = {"slug": "alice", "email": "alice@example.org", "zulip_user_id": " "}
    bound = {**unbound, "zulip_user_id": "101"}
    for check in (*UNJOINED, *UNJOINED_023, {"name": "x:allowlist", "ok": False}):
        assert ac.allowlist_pending(check, unbound)
        assert not ac.allowlist_pending(check, bound)
        assert not ac.allowlist_pending(check, {**unbound, "email": ""})
    for check in (
        {"name": "codex:allowlist", "ok": True, "detail": "101"},
        {"name": "codex:allowlist", "ok": False, "detail": NOBODY},
        {"name": "codex:allowlist", "ok": False, "detail": "no user"},
        {"name": "codex:login", "ok": False, "detail": ""},
        {"name": "allowlist", "ok": False, "detail": ""},
    ):
        assert not ac.allowlist_pending(check, unbound), check
    assert ac.pending_label(owner=True, sign_in=True) == "owner+sign-in pending"
    # the admin plan's status cell is 32 characters (`planning._cell`)
    assert len(f"active, {ac.pending_label(owner=True, sign_in=True)}") <= 32
    assert ac.pending_label(owner=True, sign_in=False) == "owner pending"
    assert ac.pending_label(owner=False, sign_in=True) == "sign-in pending"
    assert ac.pending_label(owner=False, sign_in=False) == ""
    # push_config's own table only (#458): status.py never passes it
    assert ac.pending_label(owner=True, sign_in=True, project=True) == (
        "project+owner+sign-in pending"
    )
    assert ac.pending_label(owner=False, sign_in=False, project=True) == "project pending"


def test_sign_in_checks_are_matched_by_name_not_by_message():
    assert ac.sign_in_check("codex:login")
    assert ac.sign_in_check("claude account main") and ac.sign_in_check("claude account alt")
    for other in ("codex:connection", "claude version", "claude:login", "alice:allowlist", "login"):
        assert not ac.sign_in_check(other)


def test_claude_operational_failures_are_not_sign_in_pending():
    """codex on #152: only the signed-out answer is pending; a missing config directory or a
    failed `auth status` (their own check names in the Claude doctor) stays a failure."""
    assert not ac.sign_in_check("claude config main")
    assert not ac.sign_in_check("claude auth main")
    assert ac.sign_in_check("claude account main")


#: The real release probe; an autouse fixture below stubs it for every other test, so that no
#: test depends on a locally fetched tag (codex on #167: CI's shallow checkout has none).
REAL_ARTIFACT_CONFIG = pc.artifact_config
CHECKOUT_CONFIG = (pc.CHECKOUT / "src" / "zab" / "config.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _release_is_this_checkout(monkeypatch):
    monkeypatch.setattr(pc, "artifact_config", lambda wheel, tag, runner=None: CHECKOUT_CONFIG)


CONFIG_WITH = "class BridgeConfig:\n    machine: str = ''\n    multi_party: bool = True\n"
CONFIG_WITHOUT = "class BridgeConfig:\n    machine: str = ''\n"


def fake_wheel(tmp_path, source: str):
    import zipfile

    path = tmp_path / "zulip_agent_bridge-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as whl:
        whl.writestr("zab/config.py", source)
    return str(path)


@pytest.mark.parametrize("value", [True, False])
def test_the_multi_party_switch_reaches_the_config(value):
    """PM on #36 (Mon 19:20): `--multi-party/--no-multi-party`, default on, writes
    `bridge.multi_party`: the fleet-wide kill switch for the multi-party feature (#35)."""
    import tomllib

    row = {"slug": "alice", "email": "alice@example.org", "repo": ""}
    data = tomllib.loads(pc.render_config_toml(row, multi_party=value))
    assert data["bridge"]["multi_party"] is value
    assert "multi_party" not in tomllib.loads(pc.render_config_toml(row))["bridge"]


def test_feature_support_is_read_from_the_artifact_being_installed(tmp_path, monkeypatch):
    """codex on #167: the wheel (or the tag) decides, not the admin checkout's own zab."""
    monkeypatch.setattr(pc, "artifact_config", REAL_ARTIFACT_CONFIG)
    assert pc.supports_multi_party(pc.artifact_config(fake_wheel(tmp_path, CONFIG_WITH), ""))
    other = tmp_path / "old"
    other.mkdir()
    assert not pc.supports_multi_party(pc.artifact_config(fake_wheel(other, CONFIG_WITHOUT), ""))
    assert pc.artifact_config(str(tmp_path / "missing.whl"), "") is None
    tag = lambda argv, **kw: pc.RunResult(0, CONFIG_WITH, "")  # noqa: E731
    assert pc.supports_multi_party(pc.artifact_config(None, "v9", tag))
    gone = lambda argv, **kw: pc.RunResult(128, "", "fatal: invalid object name")  # noqa: E731
    assert pc.artifact_config(None, "v0", gone) is None and not pc.supports_multi_party(None)
    assert pc.config_fields("not python(", "BridgeConfig") == set()


@pytest.mark.parametrize(
    "source,flag,expected,note",
    [
        (CONFIG_WITH, [], True, False),
        (CONFIG_WITH, ["--no-multi-party"], False, False),
        (CONFIG_WITHOUT, [], None, False),
        (CONFIG_WITHOUT, ["--no-multi-party"], None, True),
    ],
)
def test_main_sets_the_switch_only_for_a_release_that_has_it(
    tmp_path, monkeypatch, capsys, source, flag, expected, note
):
    captured = {}
    real = pc.Options
    monkeypatch.setattr(pc, "artifact_config", REAL_ARTIFACT_CONFIG)

    def options(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(pc, "Options", options)
    monkeypatch.setattr(pc, "load_roster", lambda path: [])
    pc.main(["--roster", "r.csv", "--wheel", fake_wheel(tmp_path, source), *flag, "--dry-run"])
    assert captured["multi_party"] is expected
    out, err = capsys.readouterr()
    assert ("--no-multi-party changes nothing" in err) is note
    # PM on #36 (Mon 22:30): one line says whether the switch is on, off or absent
    lines = [line for line in out.splitlines() if line.startswith("multi_party: ")]
    assert lines == [pc.multi_party_line(expected)]
    word = {True: "on", False: "OFF", None: "absent"}[expected]
    assert lines[0].split()[1] == word


def test_a_release_that_cannot_be_inspected_stops_the_rollout(tmp_path, monkeypatch, capsys):
    """codex on #167 (round 2): an unreadable wheel or an unfetched tag is unknown, not
    "no feature": nothing is rolled out, and the message says what to do."""
    monkeypatch.setattr(pc, "artifact_config", REAL_ARTIFACT_CONFIG)
    monkeypatch.setattr(pc, "load_roster", lambda path: [])
    rolled = []
    monkeypatch.setattr(pc, "run_parallel", lambda *a, **k: rolled.append(1) or [])
    code = pc.main(["--roster", "r.csv", "--wheel", str(tmp_path / "missing.whl"), "--dry-run"])
    assert code == 2 and not rolled
    assert "cannot read zab/config.py of the wheel" in capsys.readouterr().err
    monkeypatch.setattr(pc, "subprocess_runner", lambda argv, **kw: pc.RunResult(128, "", "bad"))
    monkeypatch.setattr(pc, "artifact_config", lambda wheel, tag: None)
    assert pc.main(["--roster", "r.csv", "--tag", "v9.9.9", "--dry-run"]) == 2
    assert "git fetch --tags" in capsys.readouterr().err


def test_the_rendered_config_loads_where_the_release_has_the_key(tmp_path):
    """The key is written exactly when the release's BridgeConfig has it, so the VM's zab
    (that release) never meets an unknown key."""
    import dataclasses

    from zab.config import BridgeConfig, load_config

    has = "multi_party" in {f.name for f in dataclasses.fields(BridgeConfig)}
    path = tmp_path / "config.toml"
    row = {"slug": "alice", "email": "alice@example.org", "repo": ""}
    path.write_text(pc.render_config_toml(row, multi_party=False if has else None))
    config = load_config(path)
    assert getattr(config.bridge, "multi_party", None) is (False if has else None)


# ----------------------------------------------------------------- --mention-after-s (#711)


@pytest.mark.parametrize("seconds", [0, 45, 120, 86400])
def test_the_workshops_mention_after_s_reaches_the_vms_config(tmp_path, out_dir, seconds):
    """SPEC §4.8: `[bridge] mention_after_s`, always written, 120 unless the workshop's; the
    VM's zab loads it. The config that a rollout sends is the one rendered with it."""
    from zab.config import load_config

    row = {"slug": "alice", "email": "alice@example.org", "repo": ""}
    assert tomllib.loads(pc.render_config_toml(row))["bridge"]["mention_after_s"] == 120
    path = tmp_path / "config.toml"
    path.write_text(pc.render_config_toml(row, mention_after_s=seconds))
    assert load_config(path).bridge.mention_after_s == seconds

    class Reading(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[dict] = []  # each config.toml sent, read while it is staged

        def __call__(self, argv, **kw):
            staged = [w for w in argv if w.endswith("/config.toml") and os.path.isfile(w)]
            self.sent += [tomllib.loads(pathlib.Path(w).read_text()) for w in staged]
            return super().__call__(argv, **kw)

    runner = Reading()
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH, mention_after_s=seconds)
    assert pc.rollout_vm(ALICE_ROW, opts, runner).ok
    assert [c["bridge"]["mention_after_s"] for c in runner.sent] == [seconds]


@pytest.mark.parametrize(
    "flags,seconds",
    [([], 120), (["--mention-after-s", "0"], 0), (["--mention-after-s", "300"], 300)],
)
def test_main_takes_the_workshops_mention_after_s_and_says_it(
    tmp_path, monkeypatch, capsys, flags, seconds
):
    """A hand run takes it as a flag, default 120; the line it prints is what a `!rollout`
    plan's dry run shows (#711), beside the multi-party and steering lines."""
    captured = {}
    real = pc.Options
    monkeypatch.setattr(pc, "artifact_config", REAL_ARTIFACT_CONFIG)

    def options(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(pc, "Options", options)
    monkeypatch.setattr(pc, "load_roster", lambda path: [])
    wheel = fake_wheel(tmp_path, CONFIG_WITH)
    assert pc.main(["--roster", "r.csv", "--wheel", wheel, *flags, "--dry-run"]) == 0
    assert captured["mention_after_s"] == seconds
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("mention after:")]
    assert lines == [pc.mention_after_line(seconds)]
    assert lines[0].startswith(f"mention after: {seconds} s (")
    assert ("any length" in lines[0]) is (seconds == 0)


@pytest.mark.parametrize(
    "flags,error",
    [
        (["--wheel", "w.whl", "--mention-after-s", "-1"], "takes 0 to 86400"),
        (["--wheel", "w.whl", "--mention-after-s", "86401"], "takes 0 to 86400"),
        (["--wheel", "w.whl", "--mention-after-s", "1.5"], "invalid int value"),
        (["--idle-stop", "15", "--mention-after-s", "60"], "needs a release"),
        (["--uninstall", "--mention-after-s", "60"], "needs a release"),
    ],
)
def test_main_refuses_a_mention_after_s_it_cant_write(capsys, flags, error):
    """Out of admin.toml's range, or on a run that writes no config.toml: refused, never
    dropped without a word."""
    with pytest.raises(SystemExit):
        pc.main(["--roster", "r.csv", *flags])
    assert error in capsys.readouterr().err


def test_push_config_and_admin_toml_share_the_rule():
    """One rule (#711): admin.toml's loader and a hand run take the same range and default."""
    from zab.admin import config

    assert (pc.MENTION_AFTER_S, pc.MENTION_AFTER_MAX) == (
        config.MENTION_AFTER_S,
        config.MENTION_AFTER_MAX,
    )
    from zab.config import BridgeConfig

    assert BridgeConfig().mention_after_s == pc.MENTION_AFTER_S  # the bridge's own default


def test_a_rollout_notes_a_vm_whose_instructions_name_a_missing_lean_project(out_dir):
    """Review on #261: the instructions tell agents to use lean-project, which only
    --lean-shared installs. A VM without it is noted, never failed; with --lean-shared in the
    same run there is nothing to ask."""
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    runner = FakeRunner(answers={pc.LEAN_PROJECT_PROBE: pc.LEAN_PROJECT_MISSING + "\n"})
    result = pc.rollout_vm(row, pc.Options(tag="v0.2.2", out_dir=out_dir), runner)
    assert result.ok and result.notes[-1] == (
        "lean: the instructions name lean-project, which this VM lacks: run --lean-shared"
    )
    runner = FakeRunner()
    result = pc.rollout_vm(row, pc.Options(tag="v0.2.2", out_dir=out_dir), runner)
    assert result.ok and not any(n.startswith("lean:") for n in result.notes)
    runner = FakeRunner()
    opts = pc.Options(tag="v0.2.2", out_dir=out_dir, lean_shared=True)
    pc.rollout_vm(row, opts, runner)
    assert not any(pc.LEAN_PROJECT_PROBE in r for r in runner.remotes(ALICE))


def test_a_failed_instructions_step_is_reported_and_holds_nothing_up(out_dir):
    """PM on #36 (Mon 22:30, #169 item 2): the splice leaves a file with broken markers, or
    one that is not UTF-8, alone (exit 2). The VM is reported, but the release, the services
    and the doctor ran before it, and staging is still removed."""
    left = "~/.codex/AGENTS.md left alone: markers"
    runner = FakeRunner(fail={"/usr/bin/python3 /tmp/zab-push.AbC123/agent_instructions": left})
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(tag="v0.1.0rc1", out_dir=out_dir), runner)
    assert result.installed and result.doctor_ok
    assert not result.ok and result.failures == [f"agent instructions: {left}"]
    remotes = runner.remotes(ALICE)
    assert any("uv tool install" in r for r in remotes) and pc.SERVICE_INSTALL_CMD in remotes
    assert "agent_instructions.py" in remotes[-3] and remotes[-1] == f"rm -rf -- {STAGING}"
    assert remotes[-2] == pc.warm_on_cmd(STAGING)  # the warm-up is installed all the same


@pytest.mark.parametrize(
    ("answer", "note"),
    [
        ("tools: installed /etc/zab/tools.json for nt-agents-2026-v3: 19 of 22 tools are on "
         "this VM\n", True),
        ("tools: kept the image's own /etc/zab/tools.json\n", False),
        ("tools: /etc/zab/tools.json unchanged: 22 of 22 tools are on this VM\n", False),
        ("tools: replaced /etc/zab/tools.json for nt-agents-2026-v3: 22 of 22 tools are on "
         "this VM\n", False),
    ],
)  # fmt: skip
def test_the_tools_list_is_installed_before_the_instructions(out_dir, answer, note):
    """#475: as root, before the instructions made from it; noted only the first time, when the
    rollout gives a VM the Oct 9 image's list."""
    runner = FakeRunner(answers={"tools_list.py install": answer})
    result = pc.rollout_vm(ALICE_ROW, pc.Options(tag="v0.2.6", out_dir=out_dir), runner)
    assert result.ok
    remotes = runner.remotes(ALICE)
    step = remotes.index(pc.tools_cmd(STAGING, (pc.SAGE_FEATURE,)))
    assert "agent_instructions.py" in remotes[step + 1]
    assert result.notes == ([answer.strip()] if note else [])


def test_a_failed_tools_list_is_reported_and_the_instructions_still_run(out_dir):
    runner = FakeRunner(
        fail={"tools_list.py install": "PermissionError"},
        answers={
            "agent_instructions.py": "AGENTS.md updated, CLAUDE.md unchanged\n"
            "tools: none in the instructions (/etc/zab/tools.json: missing)\n"
        },
    )
    result = pc.rollout_vm(ALICE_ROW, pc.Options(tag="v0.2.6", out_dir=out_dir), runner)
    assert result.failures == ["tools list: PermissionError"] and result.doctor_ok
    assert any("agent_instructions.py" in r for r in runner.remotes(ALICE))
    # the instructions then name no tool, and the row says why
    assert result.notes == ["tools: none in the instructions (/etc/zab/tools.json: missing)"]


def test_the_instructions_step_runs_even_when_the_upgrade_fails(out_dir):
    """Last, and independent of the release: a failed `uv tool install` still leaves the
    instructions installed."""
    runner = FakeRunner(fail={"uv tool install": "network"})
    row = {"github": "alice", "repo": "x", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, pc.Options(tag="v0.1.0rc1", out_dir=out_dir), runner)
    assert not result.installed and result.failures[0].startswith("uv tool install")
    remotes = runner.remotes(ALICE)
    assert pc.SERVICE_INSTALL_CMD not in remotes
    assert "agent_instructions.py" in remotes[-3] and remotes[-1] == f"rm -rf -- {STAGING}"


def test_public_channels_are_allowed_on_the_vm(tmp_path):
    """Drew (issue #36, Tue 06:30): participants may make their sandboxes public and add their
    agents to public channels; the VM's zab loads the rendered value."""
    from zab.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(pc.render_config_toml({"slug": "alice", "email": "a@example.org", "repo": ""}))
    assert load_config(path).bridge.allow_public_channels is True


def test_the_claude_raw_log_reaches_the_bridge_environment(tmp_path):
    """PM on issue #36 (Wed 14:45): `--claude-raw-log meta|content` sets ZAB_CLAUDE_RAW_LOG in
    the bridge's unit environment (service_env); off by default, and the VM's zab loads it."""
    from zab.config import load_config

    row = {"slug": "alice", "email": "a@example.org", "repo": ""}
    assert "ZAB_CLAUDE_RAW_LOG" not in tomllib.loads(pc.render_config_toml(row))["bridge"].get(
        "service_env", {}
    )
    path = tmp_path / "config.toml"
    path.write_text(pc.render_config_toml(row, claude_raw_log="meta"))
    env = load_config(path).bridge.service_env
    assert env["ZAB_CLAUDE_RAW_LOG"] == "meta" and "VIRTUAL_ENV" in env  # added, not replaced


STEER_WITH = "class BackendConfig:\n    kind: str = ''\n    steer: bool = False\n"


def test_claude_steering_is_on_unless_turned_off(tmp_path):
    """Drew, Oct 15 (issue #310): `main()` renders `[backends.claude] steer = true` unless
    `--no-claude-steer` (`steer = false`). Both are written, since releases up to 0.2.3 default
    to off; None (the default here, and a release without the key) leaves it out, like the
    other release-dependent keys. The VM's zab loads each."""
    from zab.config import load_config
    from zab.core.backends import make_backend

    row = {"slug": "alice", "email": "a@example.org", "repo": ""}
    rendered = tomllib.loads(pc.render_config_toml(row, claude_steer=True))
    assert rendered["backends"]["claude"]["steer"] is True
    assert "steer" not in tomllib.loads(pc.render_config_toml(row))["backends"]["claude"]
    assert pc.Options().claude_steer is None
    path = tmp_path / "config.toml"
    for steer in (True, False, None):
        path.write_text(pc.render_config_toml(row, claude_steer=steer))
        config = load_config(path)
        on = steer is not False  # absent: this checkout's default, on
        assert config.backends["claude"].steer is on
        assert make_backend("claude", config.backends["claude"]).capabilities.steer is on
        assert "steer" not in tomllib.loads(path.read_text())["backends"]["codex"]


@pytest.mark.parametrize(
    "source,flags,expected",
    [
        (STEER_WITH, [], True),
        (STEER_WITH, ["--claude-steer"], True),  # kept for existing commands: a no-op
        (STEER_WITH, ["--no-claude-steer"], False),
        (STEER_WITH, ["--claude-steer", "--no-claude-steer"], False),  # the last one wins
        (STEER_WITH, ["--no-claude-steer", "--claude-steer"], True),
        (CONFIG_WITH, [], None),  # a release without the key: left out, so off
        (CONFIG_WITH, ["--no-claude-steer"], None),
        (CONFIG_WITH, ["--claude-steer"], 2),  # asked for by name: refused, as before
    ],
)
def test_main_steers_unless_told_not_to(tmp_path, monkeypatch, capsys, source, flags, expected):
    captured = {}
    real = pc.Options
    monkeypatch.setattr(pc, "artifact_config", REAL_ARTIFACT_CONFIG)

    def options(**kw):
        captured.update(kw)
        return real(**kw)

    monkeypatch.setattr(pc, "Options", options)
    monkeypatch.setattr(pc, "load_roster", lambda path: [])
    wheel = fake_wheel(tmp_path, source)
    result = pc.main(["--roster", "r.csv", "--wheel", wheel, *flags, "--dry-run"])
    out, err = capsys.readouterr()
    assert pc.supports_claude_steer(STEER_WITH) and not pc.supports_claude_steer(CONFIG_WITH)
    if expected == 2:
        assert result == 2 and not captured and "no [backends.claude] steer key" in err
        return
    assert captured["claude_steer"] is expected
    # the summary line (issue #310): ON or OFF, whichever way it came about
    lines = [line for line in out.splitlines() if line.startswith("claude steer: ")]
    assert lines == [pc.claude_steer_line(expected)]
    assert lines[0].split()[2] == ("ON" if expected else "OFF")


@pytest.mark.parametrize("flags", [["--uninstall"], ["--idle-stop", "15"]])
def test_a_run_that_installs_no_release_has_no_steer_line(monkeypatch, capsys, flags):
    """`--uninstall` and the steps alone write no config: no line and no refusal, whatever
    the steering flag says."""
    monkeypatch.setattr(pc, "load_roster", lambda path: [])
    monkeypatch.setattr(pc, "run_parallel", lambda *a, **k: [])
    for steer in (["--claude-steer"], ["--no-claude-steer"], []):
        assert pc.main(["--roster", "r.csv", *flags, *steer, "--dry-run"]) == 0
        assert "claude steer" not in capsys.readouterr().out


# ------------------------------------------------------------------- --idle-stop (issue #207)

ALICE_ROW = {"github": "alice", "repo": "alice/thesis", "zulip_user_id": "101", "ip": ALICE}


def test_idle_stop_units_run_the_script_once_a_minute():
    units = pc.idle_stop_units(15)
    assert set(units) == {"zab-idle-stop.service", "zab-idle-stop.timer"}
    assert (
        "ExecStart=/usr/bin/python3 /usr/local/lib/zab/idle_stop.py --minutes 15\n"
        in units["zab-idle-stop.service"]
    )
    timer = units["zab-idle-stop.timer"]
    assert "OnUnitActiveSec=1min" in timer and "WantedBy=timers.target" in timer


IDLE_FILES = (
    "idle_stop.py",
    "zab-idle-stop.service",
    "zab-idle-stop.timer",
    "status_read.sh",
    "status_read_login",
    "zab-monitor.sudoers",
)


def test_rollout_with_idle_stop_installs_the_timer_last(out_dir):
    runner = FakeRunner()
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH, idle_stop=15)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok and "idle stop: after 15 min" in result.notes
    scps = [c[-1] for c in runner.calls if c[0] == "scp"]
    assert scps[-6:] == [f"ubuntu@{ALICE}:{STAGING}/{name}" for name in IDLE_FILES]
    remotes = runner.remotes(ALICE)
    assert remotes[-2] == pc.idle_stop_on_cmd(STAGING)
    assert remotes[-2].startswith(
        f"sudo install -D -o root -g root -m 0755 {STAGING}/idle_stop.py "
        "/usr/local/lib/zab/idle_stop.py && "
    )
    assert (  # the timer on before the read user (#507), whose failure leaves it on; then
        # idle stop is no longer off on purpose (#704)
        "sudo systemctl daemon-reload && sudo systemctl enable --now zab-idle-stop.timer "
        "&& systemctl is-active zab-idle-stop.timer && sudo rm -f /etc/zab/idle-stop.off "
        "&& sudo install " in remotes[-2]
    )
    assert remotes[-2].endswith("/usr/local/lib/zab/idle_stop.py --setup-read-user")
    assert remotes[-1] == f"rm -rf -- {STAGING}"


def test_idle_stop_alone_touches_nothing_else(out_dir):
    runner = FakeRunner()
    opts = pc.Options(known_hosts=KH, idle_stop=15)
    result = pc.steps_vm(ALICE_ROW, opts, runner)
    assert result.ok and result.notes == ["idle stop: after 15 min"]
    remotes = runner.remotes(ALICE)
    assert remotes == [
        pc.OWNER_CMD,
        pc.MKTEMP_CMD,
        pc.idle_stop_on_cmd(STAGING),
        f"rm -rf -- {STAGING}",
    ]
    assert tuple(c[-1].rsplit("/", 1)[1] for c in runner.calls if c[0] == "scp") == IDLE_FILES


def test_idle_stop_zero_removes_the_timer(out_dir):
    runner = FakeRunner()
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, idle_stop=0), runner)
    assert result.ok and result.notes == ["idle stop: off"]
    assert runner.remotes(ALICE) == [pc.OWNER_CMD, pc.IDLE_STOP_OFF_CMD]
    assert "disable --now zab-idle-stop.timer" in pc.IDLE_STOP_OFF_CMD
    # the read user goes too (#507): a tick under way ends first (it may be rewriting the keys
    # that rm -rf removes), then the rule, the scripts and the keys, then the user even while a
    # read runs (its user@.service outlives a read by about 10 s)
    off = pc.IDLE_STOP_OFF_CMD.split("; ")
    assert off[:2] == [
        "sudo systemctl disable --now zab-idle-stop.timer 2>/dev/null",
        "sudo systemctl stop zab-idle-stop.service 2>/dev/null",
    ]
    rest = off[2].split(" && ")
    assert rest[0].endswith(
        " /etc/sudoers.d/zab-monitor /usr/local/lib/zab/status_read.sh "
        "/usr/local/lib/zab/status_read_login"
    )
    assert rest[1:3] == [
        "sudo rm -rf /var/lib/zab-monitor",
        "{ sudo userdel --force zab-monitor 2>/dev/null",
    ]


def test_idle_stop_sets_up_the_read_user(tmp_path):
    """#507: the read and its rule, checked by visudo first, then the user and its keys."""
    cmd = pc.idle_stop_on_cmd(STAGING).split(" && ")
    rule = f"{STAGING}/zab-monitor.sudoers"
    assert cmd[-5:] == [
        f"sudo install -o root -g root -m 0755 {STAGING}/status_read.sh "
        "/usr/local/lib/zab/status_read.sh",
        f"sudo install -o root -g root -m 0755 {STAGING}/status_read_login "
        "/usr/local/lib/zab/status_read_login",
        f"sudo visudo -cqf {rule}",
        f"sudo install -o root -g root -m 0440 {rule} /etc/sudoers.d/zab-monitor",
        "sudo /usr/bin/python3 /usr/local/lib/zab/idle_stop.py --setup-read-user",
    ]
    files = pc.read_user_files()
    assert files["status_read.sh"] == st.READ_SCRIPT
    assert files["status_read_login"] == idle_stop.READ_LOGIN_TEXT
    # the forced command (idle_stop.READ_OPTIONS) is exactly what the rule allows: no arguments
    sudo = idle_stop.READ_OPTIONS.split('command="', 1)[1].rstrip('"')
    assert sudo == "/usr/bin/sudo -n -H -u agent /usr/local/lib/zab/status_read.sh"
    rule_line = files["zab-monitor.sudoers"].splitlines()[-1]
    assert rule_line == 'zab-monitor ALL=(agent) NOPASSWD: /usr/local/lib/zab/status_read.sh ""'
    assert idle_stop.READ_USER == st.READ_USER == "zab-monitor"
    visudo = shutil.which("visudo") or "/usr/sbin/visudo"
    if os.path.exists(visudo):  # sudo's own parser, where it is installed
        (tmp_path / "rule").write_text(files["zab-monitor.sudoers"])
        assert subprocess.run([visudo, "-cqf", str(tmp_path / "rule")]).returncode == 0  # noqa: S603


def test_idle_stop_on_someone_elses_vm_does_nothing():
    runner = FakeRunner(owners={ALICE: owner_stdout("bob")})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, idle_stop=15), runner)
    assert not result.ok and "belongs to 'bob'" in result.failures[0]
    assert runner.remotes(ALICE) == [pc.OWNER_CMD]


def test_a_failed_idle_stop_is_a_failure(out_dir):
    runner = FakeRunner(fail={"enable --now zab-idle-stop.timer": "Failed to enable unit"})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, idle_stop=15), runner)
    assert not result.ok and result.failures == ["idle stop: Failed to enable unit"]
    assert runner.remotes(ALICE)[-1] == f"rm -rf -- {STAGING}"  # staging still cleaned up


def test_main_idle_stop_without_a_release(roster, out_dir, monkeypatch, capsys):
    runner = FakeRunner()
    monkeypatch.setattr(pc, "subprocess_runner", runner)
    monkeypatch.setattr(pc, "artifact_config", lambda *a: pytest.fail("no release to read"))
    code = pc.main(["--roster", str(roster), "--idle-stop", "15", "--only", "alice"])
    assert code == 0
    out = capsys.readouterr().out
    assert "idle stop" in out and "note: idle stop: after 15 min" in out
    assert not any("uv tool install" in r for r in runner.remotes(ALICE))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--idle-stop", "-1"], "takes at least 10 minutes"),
        (["--idle-stop", "5"], "takes at least 10 minutes"),
        (["--idle-stop", "15", "--uninstall"], "can't be combined"),
        ([], "name the release"),
    ],
)
def test_main_idle_stop_arguments(roster, extra, message, capsys):
    with pytest.raises(SystemExit):
        pc.main(["--roster", str(roster), *extra])
    assert message in capsys.readouterr().err


def test_a_failed_restart_still_runs_the_doctor_and_says_why(out_dir):
    """PM on #211, P3: with Type=notify the restart fails when the bridge doesn't start; the
    doctor still runs, and the row carries the restart's error next to its checks."""

    class FailingRestart(FakeRunner):
        def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
            if argv[0] == "ssh" and argv[-1] == pc.SERVICE_INSTALL_CMD:
                self.calls.append(list(argv))
                out = (
                    f"{pc.SERVICE_FAILED_MARK}\n"
                    '[{"name": "service:zab", "ok": false, "detail": "failed"}]\n'
                )
                err = "Job for zab.service failed because the control process exited.\n"
                return ac.RunResult(1, out, err)
            return super().__call__(argv, timeout_s=timeout_s, env=env, stdin=stdin)

    runner = FailingRestart()
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH)
    row = {"github": "alice", "repo": "alice/thesis", "zulip_user_id": "101", "ip": ALICE}
    result = pc.rollout_vm(row, opts, runner)
    assert not result.ok
    assert result.failures[0] == (
        "service install / restart: Job for zab.service failed because the control process exited."
    )
    assert [c["name"] for c in result.doctor] == ["service:zab"]  # the doctor still ran
    assert "doctor: service:zab: failed" in result.failures


# ------------------------------------------------------- --restart-codex (issue #214 migration)


class StatusRunner(FakeRunner):
    """A VM whose `zab status --json` reports `status`, and whose daemon restart may fail."""

    def __init__(self, status: str, *, restart_fails: bool = False):
        super().__init__()
        self.status = status
        self.restart_fails = restart_fails

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        if argv[0] == "ssh" and argv[-1] == pc.CODEX_STATUS_CMD:
            self.calls.append(list(argv))
            return ac.RunResult(0, self.status, "")
        if argv[0] == "ssh" and argv[-1] == pc.CODEX_RESTART_CMD and self.restart_fails:
            self.calls.append(list(argv))
            return ac.RunResult(1, "", "Failed to restart codex-app-server.service")
        return super().__call__(argv, timeout_s=timeout_s, env=env, stdin=stdin)


def zab_status(*turns: tuple[str, str]) -> str:
    """`zab status --json` with one running turn per (session id, backend)."""
    sessions = [{"id": sid, "backend": backend} for sid, backend in turns]
    sessions.append({"id": "idle", "backend": "codex"})
    running = [{"session_id": sid} for sid, _ in turns]
    return json.dumps({"sessions": sessions, "running_turns": running}) + "\n"


def test_restart_codex_restarts_an_idle_daemon_before_the_bridge(out_dir):
    runner = StatusRunner(zab_status(("s1", "claude")))  # only a Claude turn runs
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH, restart_codex=True)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok and "codex daemon restarted" in result.notes
    remotes = runner.remotes(ALICE)
    i = remotes.index(pc.CODEX_RESTART_CMD)
    # the old bridge stops first, so it can't reopen threads on the restarted daemon (codex #234)
    assert remotes[i - 2 : i + 2] == [
        pc.BRIDGE_STOP_CMD,
        pc.CODEX_STATUS_CMD,
        pc.CODEX_RESTART_CMD,
        pc.SERVICE_INSTALL_CMD,
    ]
    # the service step starts the new bridge; `start` afterwards is a no-op that guarantees it
    assert remotes[i + 2] == pc.BRIDGE_START_CMD
    assert pc.CODEX_RESTART_CMD.endswith(
        "bash -lc 'systemctl --user restart \"codex-app-server*.service\"'"
    )


@pytest.mark.parametrize(
    ("status", "note"),
    [
        (zab_status(("s1", "codex"), ("s2", "codex")), "2 Codex turn(s) running"),
        ('{"error": "OperationalError"}\n', "zab status unreadable"),
        ("not json\n", "zab status unreadable"),
    ],
)
def test_restart_codex_leaves_a_busy_or_unknown_daemon_alone(out_dir, status, note):
    runner = StatusRunner(status)
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH, restart_codex=True)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok  # a note, never a failure: the rest of the rollout is in place
    assert any(note in n and "NOT restarted" in n for n in result.notes)
    remotes = runner.remotes(ALICE)
    assert pc.CODEX_RESTART_CMD not in remotes
    assert remotes.index(pc.BRIDGE_STOP_CMD) < remotes.index(pc.SERVICE_INSTALL_CMD)


def test_a_bridge_that_wont_stop_keeps_its_daemon(out_dir):
    runner = StatusRunner(zab_status())
    runner.fail = {"systemctl --user stop zab.service": "Failed to stop zab.service"}
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH, restart_codex=True)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert "codex daemon NOT restarted: the bridge didn't stop (Failed to stop zab.service)" in (
        result.notes
    )
    remotes = runner.remotes(ALICE)
    assert pc.CODEX_STATUS_CMD not in remotes and pc.CODEX_RESTART_CMD not in remotes


def test_a_stopped_bridge_is_always_started_again(out_dir):
    """Whatever the service step did, a bridge stopped for --restart-codex is started again."""
    for fail in ({"zab service install": "boom"}, {}):
        runner = StatusRunner(zab_status())
        runner.fail = fail
        opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH, restart_codex=True)
        pc.rollout_vm(ALICE_ROW, opts, runner)
        remotes = runner.remotes(ALICE)
        assert remotes.count(pc.BRIDGE_START_CMD) == 1
        assert remotes.index(pc.SERVICE_INSTALL_CMD) < remotes.index(pc.BRIDGE_START_CMD)


def test_a_failed_daemon_restart_is_reported(out_dir):
    runner = StatusRunner(zab_status(), restart_fails=True)
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH, restart_codex=True)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok
    assert "codex daemon restart failed: Failed to restart codex-app-server.service" in (
        result.notes
    )


def test_without_the_flag_the_daemon_is_not_touched(out_dir):
    runner = StatusRunner(zab_status())
    pc.rollout_vm(ALICE_ROW, pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH), runner)
    remotes = runner.remotes(ALICE)
    assert pc.CODEX_STATUS_CMD not in remotes and pc.CODEX_RESTART_CMD not in remotes


@pytest.mark.parametrize(
    "extra",
    [
        ["--restart-codex"],
        ["--restart-codex", "--uninstall"],
        ["--restart-codex", "--idle-stop", "15"],
    ],
)
def test_restart_codex_needs_a_release(roster, extra, capsys):
    with pytest.raises(SystemExit):
        pc.main(["--roster", str(roster), *extra])
    assert "--restart-codex needs a release" in capsys.readouterr().err


def test_a_rollout_holds_idle_stop_off_from_its_first_change_to_its_end(out_dir):
    """PM on #218: a tick between push_config's SSH connections could power the VM off."""
    runner = FakeRunner()
    opts = pc.Options(tag="v0.2.0", out_dir=out_dir, known_hosts=KH)
    pc.rollout_vm(ALICE_ROW, opts, runner)
    remotes = runner.remotes(ALICE, holds=True)
    assert remotes[2] == pc.IDLE_HOLD_ON_CMD  # after the owner check and the staging directory
    assert remotes[-2:] == [f"rm -rf -- {STAGING}", pc.IDLE_HOLD_OFF_CMD]
    assert "3600" in pc.IDLE_HOLD_ON_CMD and "/run/zab-idle-stop.hold" in pc.IDLE_HOLD_OFF_CMD
    # a failed step still lifts the hold
    runner = FakeRunner(fail={"tool install": "error"})
    pc.rollout_vm(ALICE_ROW, opts, runner)
    assert runner.remotes(ALICE, holds=True)[-1] == pc.IDLE_HOLD_OFF_CMD
    # and installing idle stop alone holds it too
    runner = FakeRunner()
    pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, idle_stop=15), runner)
    remotes = runner.remotes(ALICE, holds=True)
    assert remotes.count(pc.IDLE_HOLD_ON_CMD) == 1 and remotes[-1] == pc.IDLE_HOLD_OFF_CMD


def test_the_idle_stop_service_is_bounded():
    assert "TimeoutStartSec=120" in pc.idle_stop_units(15)["zab-idle-stop.service"]


HOOK_WITH = (
    "class BridgeConfig:\n    machine: str = ''\n"
    "    worktree_post_create: list[str] = field(default_factory=list)\n"
)


def test_the_worktree_hook_is_written_only_for_releases_that_have_it(tmp_path):
    """Issue #205: new topic worktrees run `lean-project --repair-all`; a release without
    the key gets no key (zab refuses unknown keys), and the VM's zab loads the profile."""
    from zab.config import load_config

    row = {"github": "alice", "repo": "alice/thesis", "zulip_user_id": "101"}
    text = pc.render_config_toml(row, worktree_hook=True)
    assert tomllib.loads(text)["bridge"]["worktree_post_create"] == [
        "/usr/local/bin/lean-project",
        "--repair-all",
    ]
    assert "worktree_post_create" not in tomllib.loads(pc.render_config_toml(row))["bridge"]
    path = tmp_path / "config.toml"
    path.write_text(text)
    assert load_config(path).bridge.worktree_post_create == pc.WORKTREE_POST_CREATE
    assert pc.supports_worktree_post_create(HOOK_WITH)
    assert not pc.supports_worktree_post_create(CONFIG_WITH)
    assert not pc.supports_worktree_post_create(None)


# ---------------------------------------------- the page-cache warm-up after each boot (#461)


class Uploads(FakeRunner):
    """Also keeps what each `scp` sends, read when it runs (the staging files are temporary)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent: dict[str, str] = {}

    def __call__(self, argv, **kw):
        if argv[0] == "scp":
            self.sent[argv[-1].rsplit("/", 1)[1]] = pathlib.Path(argv[-2]).read_text()
        return super().__call__(argv, **kw)


def test_the_warm_up_unit_runs_the_script_once_per_boot_under_read_caps():
    unit = pc.warm_unit()
    lines = unit.splitlines()
    assert "Type=oneshot" in lines
    assert "ExecStart=/usr/bin/python3 /usr/local/lib/zab/warm_cache.py" in lines
    # after the boot's other units, and pulled in by every boot
    assert "After=multi-user.target" in lines and "WantedBy=multi-user.target" in lines
    # cgroup v2 caps, which hold whatever the scheduler: the root file system's disk, and
    # Nitro's root disk by name, whichever systemd resolves. systemd's M is 1000², so this is
    # 80 MB/s, about 60% of gp3's 131 MB/s (review round 1 on #464)
    for device in ("/", "/dev/nvme0n1"):
        assert f"IOReadBandwidthMax={device} 80M" in lines
        assert f"IOReadIOPSMax={device} 1500" in lines
    assert "Nice=19" in lines and "IOSchedulingClass=best-effort" in lines
    assert "IOSchedulingPriority=7" in lines  # never `idle`: it must not starve behind Lean
    assert "TimeoutStartSec=30min" in lines
    # its own memory is bounded (review round 1), never by MemoryMax=: the page cache it fills
    # is charged to its cgroup, so a cgroup limit would evict what it read
    assert "LimitAS=2G" in lines and "Memory" not in unit


def test_every_rollout_installs_the_warm_up_after_lean_and_before_idle_stop(out_dir):
    runner = Uploads()
    opts = pc.Options(tag="v0.2.6", out_dir=out_dir, known_hosts=KH, lean_shared=True, idle_stop=15)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok and not any("warm" in n for n in result.notes)  # quiet on success
    remotes = runner.remotes(ALICE)
    warm = remotes.index(pc.warm_on_cmd(STAGING))
    assert remotes.index(pc.lean_check_cmd()) < warm < remotes.index(pc.idle_stop_on_cmd(STAGING))
    assert runner.sent["zab-warm-cache.service"] == pc.warm_unit()
    assert runner.sent["warm_cache.py"] == pc.WARM_SCRIPT.read_text()
    assert pc.warm_on_cmd(STAGING) == (
        f"sudo install -D -o root -g root -m 0755 {STAGING}/warm_cache.py "
        "/usr/local/lib/zab/warm_cache.py && "
        f"sudo install -o root -g root -m 0644 {STAGING}/zab-warm-cache.service "
        "/etc/systemd/system/zab-warm-cache.service && sudo systemctl daemon-reload && "
        # enabled for the next boots, not started now; the dry run checks that the script runs
        "sudo systemctl enable --quiet zab-warm-cache.service && "
        "sudo /usr/bin/python3 /usr/local/lib/zab/warm_cache.py --dry-run"
    )


def test_a_failed_warm_up_is_a_failure_that_holds_nothing_else_up(out_dir):
    runner = FakeRunner(fail={"warm_cache.py --dry-run": "SyntaxError"})
    opts = pc.Options(tag="v0.2.6", out_dir=out_dir, known_hosts=KH, idle_stop=15)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.installed and result.doctor_ok and result.failures == ["warm-up: SyntaxError"]
    assert not result.ok and "idle stop: after 15 min" in result.notes  # idle stop still runs
    assert runner.remotes(ALICE)[-1] == f"rm -rf -- {STAGING}"


def test_steps_without_a_release_leave_the_warm_up_alone():
    runner = FakeRunner()
    pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_shared=True, idle_stop=15), runner)
    assert not any("warm" in r for r in runner.remotes(ALICE))
    assert not any("warm" in c[-1] for c in runner.calls if c[0] == "scp")


def test_uninstall_removes_the_warm_up_even_after_a_failed_service_uninstall(out_dir):
    assert pc.WARM_OFF_CMD == (
        "sudo systemctl disable --now zab-warm-cache.service 2>/dev/null; "
        "sudo rm -f /etc/systemd/system/zab-warm-cache.service /usr/local/lib/zab/warm_cache.py "
        "&& sudo systemctl daemon-reload"
    )
    opts = pc.Options(out_dir=out_dir, uninstall=True)
    runner = FakeRunner(fail={"zab service uninstall": "no unit"})
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert runner.remotes(ALICE) == [pc.SERVICE_UNINSTALL_CMD, pc.WARM_OFF_CMD]
    assert result.failures == ["service uninstall: no unit"]
    runner = FakeRunner(fail={"rm -f /etc/systemd/system/zab-warm-cache": "read-only"})
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert not result.ok and result.failures == ["warm-up removal: read-only"]


def test_the_uninstall_dry_run_line(roster, out_dir, monkeypatch, capsys):
    monkeypatch.setattr(pc, "subprocess_runner", FakeRunner())
    base = ["--roster", str(roster), "--out-dir", str(out_dir), "--only", "alice", "--dry-run"]
    assert pc.main([*base, "--uninstall"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("dry-run")]
    assert lines == [f"dry-run alice ({ALICE}): service uninstall; warm-up removed"]


# ----------------------------------------------- --lean-shared: one shared Mathlib (issue #205)

LEAN_CHECKED = (
    "lean-project: Mathlib.Tactic file checked in 14.2 s; comparator accepted a Mathlib "
    "solution through the shared packages\nlean-project: Mathlib.Tactic file checked in 2.9 s\n"
)


def test_lean_shared_alone_installs_the_tool_shares_mathlib_and_checks_it():
    runner = FakeRunner(answers={"lean-project --check": LEAN_CHECKED})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_shared=True), runner)
    assert result.ok
    assert result.notes == ["lean: shared Mathlib: a Mathlib file in 14.2 s, then 2.9 s"]
    assert runner.remotes(ALICE) == [
        pc.OWNER_CMD,
        pc.MKTEMP_CMD,
        pc.lean_shared_on_cmd(STAGING),
        pc.lean_check_cmd(),
        f"rm -rf -- {STAGING}",
    ]
    assert [c[-1] for c in runner.calls if c[0] == "scp"] == [
        f"ubuntu@{ALICE}:{STAGING}/lean-project"
    ]
    assert pc.lean_shared_on_cmd(STAGING) == (
        f"sudo install -D -o root -g root -m 0755 {STAGING}/lean-project "
        "/usr/local/bin/lean-project && sudo /usr/bin/python3 /usr/local/bin/lean-project "
        "--install /home/agent/lean/workspace"
    )
    check = pc.lean_check_cmd()
    assert check.startswith("sudo -u agent -H bash -lc ")  # as agent, with ~/.elan/bin
    assert "lean-project --check --comparator --max-seconds 120" in check
    assert pc.LEAN_PROJECT_SCRIPT.is_file()
    assert any(c[-1] == pc.IDLE_HOLD_ON_CMD for c in runner.calls)  # held up while it runs


def test_lean_shared_runs_before_the_idle_stop_timer(out_dir):
    runner = FakeRunner()
    opts = pc.Options(known_hosts=KH, lean_shared=True, idle_stop=15)
    result = pc.steps_vm(ALICE_ROW, opts, runner)
    assert result.ok and result.notes == ["lean: shared Mathlib", "idle stop: after 15 min"]
    remotes = runner.remotes(ALICE)
    assert remotes.index(pc.lean_check_cmd()) < remotes.index(pc.idle_stop_on_cmd(STAGING))
    runner = FakeRunner()
    opts = pc.Options(tag="v0.2.2", out_dir=out_dir, known_hosts=KH, lean_shared=True, idle_stop=15)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert result.ok and "lean: shared Mathlib" in result.notes
    remotes = runner.remotes(ALICE)
    assert remotes.index(pc.lean_check_cmd()) < remotes.index(pc.idle_stop_on_cmd(STAGING))


def test_a_refused_lean_install_is_a_failure_and_skips_the_check():
    refusal = "lean-project: refused: different filesystems; moving would copy gigabytes"
    runner = FakeRunner(fail={"--install /home/agent/lean/workspace": refusal})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_shared=True), runner)
    assert not result.ok and result.failures == [f"lean shared: {refusal}"]
    assert pc.lean_check_cmd() not in runner.remotes(ALICE)
    assert runner.remotes(ALICE)[-1] == f"rm -rf -- {STAGING}"


MOVED = (
    "lean-project: moved /home/agent/lean/workspace/.lake/packages to "
    "/opt/lean/packages/v4.34.1; read-only; template /opt/lean/template/v4.34.1\n"
)


def test_a_failed_lean_check_right_after_the_move_moves_the_packages_back():
    """Review on #259: the check used to report a failure only after the move, which stayed;
    now the workspace gets its packages back, so Lean on the VM works as before."""
    runner = FakeRunner(
        answers={"--install /home/agent/lean/workspace": MOVED},
        fail={"lean-project --check": "lean-project: lake env lean failed: boom"},
    )
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_shared=True), runner)
    assert not result.ok and result.failures == [
        "lean check: lean-project: lake env lean failed: boom"
    ]
    # and the instructions now name a lean-project that refuses new projects (review on #262)
    assert result.notes == [
        "lean: rolled back; ~/lean/workspace has its own Mathlib again",
        pc.LEAN_ROLLED_BACK_WARNING,
    ]
    assert "instructions name lean-project" in pc.LEAN_ROLLED_BACK_WARNING
    remotes = runner.remotes(ALICE)
    assert remotes.index(pc.lean_check_cmd()) + 1 == remotes.index(pc.LEAN_SHARED_UNDO_CMD)
    assert pc.LEAN_SHARED_UNDO_CMD == (
        "sudo /usr/bin/python3 /usr/local/bin/lean-project --uninstall /home/agent/lean/workspace"
    )
    # a failed rollback is a failure of its own
    runner = FakeRunner(
        answers={"--install /home/agent/lean/workspace": MOVED},
        fail={"lean-project --check": "boom", "--uninstall": "the write bits are unknown"},
    )
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_shared=True), runner)
    assert result.failures == ["lean check: boom", "lean rollback: the write bits are unknown"]


def test_a_rollout_whose_lean_shared_rolls_back_warns_about_the_new_instructions(out_dir):
    """Review on #262: the rollout installs instructions that name lean-project, and then the
    shared copy it would use is rolled back."""
    runner = FakeRunner(
        answers={"--install /home/agent/lean/workspace": MOVED},
        fail={"lean-project --check": "boom"},
    )
    opts = pc.Options(tag="v0.2.2", out_dir=out_dir, known_hosts=KH, lean_shared=True)
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    remotes = runner.remotes(ALICE)
    assert any("agent_instructions.py" in r for r in remotes)  # installed in this run
    assert result.notes[-2:] == [
        "lean: rolled back; ~/lean/workspace has its own Mathlib again",
        pc.LEAN_ROLLED_BACK_WARNING,
    ]


def test_a_failed_lean_check_leaves_a_copy_shared_before_this_run():
    """Projects may link to a copy shared in an earlier run: it is never moved back."""
    runner = FakeRunner(
        answers={"--install /home/agent/lean/workspace": "lean-project: already shared; ..."},
        fail={"lean-project --check": "boom"},
    )
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_shared=True), runner)
    assert result.failures == ["lean check: boom"]
    assert result.notes == ["lean: the shared copy predates this run, so it stays"]
    assert pc.LEAN_SHARED_UNDO_CMD not in runner.remotes(ALICE)


def test_main_lean_shared_without_a_release(roster, monkeypatch, capsys):
    runner = FakeRunner()
    monkeypatch.setattr(pc, "subprocess_runner", runner)
    monkeypatch.setattr(pc, "artifact_config", lambda *a: pytest.fail("no release to read"))
    assert pc.main(["--roster", str(roster), "--lean-shared", "--only", "alice"]) == 0
    out = capsys.readouterr().out
    assert "steps" in out and "note: lean: shared Mathlib" in out
    assert not any("uv tool install" in r for r in runner.remotes(ALICE))
    with pytest.raises(SystemExit):
        pc.main(["--roster", str(roster), "--lean-shared", "--uninstall"])
    assert "can't be combined with --uninstall" in capsys.readouterr().err


RECLAIM_REPORT = (
    "lean-project: /home/agent/work/thesis/.zab/worktrees/a/lean: 8.2 GB would be freed\n"
    "lean-project: /home/agent/work/thesis/.zab/worktrees/b/lean: 8.1 GB would be freed\n"
    "lean-project: /w/c/lean: its own copy is on other pins: kept\n"
    "lean-project: total: 16.3 GB to free (a dry run: --apply frees it)\n"
)


def test_lean_reclaim_report_changes_nothing_and_says_how_much():
    runner = FakeRunner(answers={"lean-project --reclaim": RECLAIM_REPORT})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="report"), runner)
    assert result.ok and result.notes == [
        "lean reclaim: 2 project(s), total: 16.3 GB to free (a dry run: --apply frees it)"
    ]
    assert runner.remotes(ALICE) == [pc.OWNER_CMD, pc.lean_reclaim_cmd(False)]
    assert "lean-project --reclaim /home/agent/work" in pc.lean_reclaim_cmd(False)
    assert pc.lean_reclaim_cmd(False).startswith("sudo -u agent -H bash -lc ")
    assert any(c[-1] == pc.IDLE_HOLD_ON_CMD for c in runner.calls)


def test_lean_reclaim_apply_runs_only_when_no_turn_is_running():
    idle = '{"sessions": [], "running_turns": []}\n'
    freed = "lean-project: /w/a/lean: freed 8.2 GB\nlean-project: total: 8.2 GB freed\n"
    runner = FakeRunner(answers={"zab status --json": idle, "lean-project --reclaim": freed})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="apply"), runner)
    assert result.ok and result.notes == ["lean reclaim: 1 project(s), total: 8.2 GB freed"]
    # no turn can start while packages are checked and deleted (codex round 6 on #262)
    assert runner.remotes(ALICE)[-6:] == [
        pc.CODEX_STATUS_CMD,  # don't interrupt a running Claude turn
        pc.BRIDGE_STATE_CMD,  # a bridge that isn't running is left so (review on #262)
        pc.BRIDGE_STOP_CMD,
        pc.CODEX_STATUS_CMD,  # a Codex turn started meanwhile runs on (codex round 7)
        pc.lean_reclaim_cmd(True),
        pc.BRIDGE_START_CMD,
    ]
    assert "--reclaim --apply /home/agent/work" in pc.lean_reclaim_cmd(True)
    busy = '{"sessions": [], "running_turns": [{"session_id": 1}]}\n'
    runner = FakeRunner(answers={"zab status --json": busy})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="apply"), runner)
    assert result.ok and result.notes == [
        "lean reclaim: not applied (1 turn(s) running); rerun it later"
    ]
    assert pc.lean_reclaim_cmd(True) not in runner.remotes(ALICE)
    runner = FakeRunner(answers={"zab status --json": "not json"})
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="apply"), runner)
    assert result.notes == ["lean reclaim: not applied (zab status unreadable); rerun it later"]


def test_lean_reclaim_follows_lean_shared_in_one_run():
    runner = FakeRunner()
    opts = pc.Options(known_hosts=KH, lean_shared=True, lean_reclaim="report")
    result = pc.steps_vm(ALICE_ROW, opts, runner)
    remotes = runner.remotes(ALICE)
    assert remotes.index(pc.lean_check_cmd()) < remotes.index(pc.lean_reclaim_cmd(False))
    assert result.ok


def test_no_reclaim_after_a_failed_lean_shared_in_the_same_run(out_dir):
    """Review on #262: working private copies were replaced by links to a shared copy whose
    install or check had just failed."""
    idle = '{"sessions": [], "running_turns": []}\n'
    for fail in ({"--install /home/agent/lean/workspace": "refused"}, {"--check": "boom"}):
        runner = FakeRunner(answers={"zab status --json": idle}, fail=fail)
        opts = pc.Options(known_hosts=KH, lean_shared=True, lean_reclaim="apply")
        result = pc.steps_vm(ALICE_ROW, opts, runner)
        assert not result.ok and pc.LEAN_RECLAIM_SKIPPED in result.notes
        assert not any("--reclaim" in r for r in runner.remotes(ALICE))
        assert pc.BRIDGE_STOP_CMD not in runner.remotes(ALICE)
    runner = FakeRunner(answers={"zab status --json": idle}, fail={"--check": "boom"})
    opts = pc.Options(
        tag="v0.2.2", out_dir=out_dir, known_hosts=KH, lean_shared=True, lean_reclaim="apply"
    )
    result = pc.rollout_vm(ALICE_ROW, opts, runner)
    assert pc.LEAN_RECLAIM_SKIPPED in result.notes
    assert not any("--reclaim" in r for r in runner.remotes(ALICE))


class BridgeState(FakeRunner):
    """`systemctl --user is-active zab.service` answers `state` (exit 3 unless active)."""

    def __init__(self, state: str, **kw):
        super().__init__(**kw)
        self.state = state

    def __call__(self, argv, **kw):
        if argv[0] == "ssh" and argv[-1] == pc.BRIDGE_STATE_CMD:
            self.calls.append(list(argv))
            self.stdins.append(None)
            return ac.RunResult(0 if self.state == "active" else 3, self.state + "\n", "")
        return super().__call__(argv, **kw)


def test_reclaim_leaves_a_stopped_bridge_stopped():
    """Review on #262: `finally` started the bridge even when it hadn't been running."""
    idle = '{"sessions": [], "running_turns": []}\n'
    for state in ("inactive", "failed"):
        runner = BridgeState(state, answers={"zab status --json": idle})
        result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="apply"), runner)
        remotes = runner.remotes(ALICE)
        assert result.ok and pc.lean_reclaim_cmd(True) in remotes
        assert pc.BRIDGE_STOP_CMD not in remotes and pc.BRIDGE_START_CMD not in remotes
    # an unclear answer is no reason to leave it running: it is stopped and started
    runner = FakeRunner(answers={"zab status --json": idle}, fail={"is-active": "ssh: timeout"})
    pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="apply"), runner)
    remotes = runner.remotes(ALICE)
    assert remotes.index(pc.BRIDGE_STOP_CMD) < remotes.index(pc.lean_reclaim_cmd(True))
    assert remotes.index(pc.lean_reclaim_cmd(True)) < remotes.index(pc.BRIDGE_START_CMD)


def test_main_lean_reclaim_arguments(roster, monkeypatch, capsys):
    runner = FakeRunner()
    monkeypatch.setattr(pc, "subprocess_runner", runner)
    assert pc.main(["--roster", str(roster), "--lean-reclaim", "report", "--only", "alice"]) == 0
    assert "note: lean reclaim:" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        pc.main(["--roster", str(roster), "--lean-reclaim", "apply", "--uninstall"])
    with pytest.raises(SystemExit):
        pc.main(["--roster", str(roster), "--lean-reclaim", "everything"])


def test_a_failed_reclaim_still_starts_the_bridge_again():
    idle = '{"sessions": [], "running_turns": []}\n'
    runner = FakeRunner(
        answers={"zab status --json": idle}, fail={"lean-project --reclaim": "disk error"}
    )
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="apply"), runner)
    assert not result.ok and result.failures == ["lean reclaim: disk error"]
    assert runner.remotes(ALICE)[-1] == pc.BRIDGE_START_CMD
    # the report never stops the bridge
    runner = FakeRunner()
    pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="report"), runner)
    assert pc.BRIDGE_STOP_CMD not in runner.remotes(ALICE)


class TurnStartsMeanwhile(FakeRunner):
    """`zab status` shows no turn until the bridge has stopped, then a Codex one."""

    def __call__(self, argv, **kw):
        if argv[0] == "ssh" and argv[-1] == pc.CODEX_STATUS_CMD:
            self.calls.append(list(argv))
            self.stdins.append(None)
            stopped = any(c[-1] == pc.BRIDGE_STOP_CMD for c in self.calls)
            turns = '[{"session_id": 1}]' if stopped else "[]"
            return ac.RunResult(0, f'{{"sessions": [], "running_turns": {turns}}}\n', "")
        return super().__call__(argv, **kw)


def test_a_turn_that_started_before_the_stop_cancels_the_reclaim():
    runner = TurnStartsMeanwhile()
    result = pc.steps_vm(ALICE_ROW, pc.Options(known_hosts=KH, lean_reclaim="apply"), runner)
    assert result.ok and result.notes == [
        "lean reclaim: not applied (1 turn(s) running once the bridge stopped); rerun it later"
    ]
    remotes = runner.remotes(ALICE)
    assert pc.lean_reclaim_cmd(True) not in remotes and remotes[-1] == pc.BRIDGE_START_CMD


# ------------------------------------------------- the test rig's one-row roster (issue #263)


def test_one_row_test_roster_rolls_out_with_only_and_steer(tmp_path, monkeypatch, capsys):
    """The rig (#263 setup step 5): `--roster ~/.config/zab/test-roster.csv --wheel … --only
    zab-test --claude-steer`, with a one-row roster whose slug is not a GitHub login."""
    ip = "198.51.100.20"
    roster = tmp_path / "test-roster.csv"
    roster.write_text(
        "slug,first_name,last_name,email,host,ip,zulip_user_id\n"
        f"zab-test,Zab,Tester,zab-tester@example.org,ws-zab-test,{ip},901\n"
    )
    out = tmp_path / "out"
    for kind in ("codex", "claude"):
        p = out / "zab-test" / f"{kind}.zuliprc"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[api]\nemail={kind}-zab-test-bot@org\nkey={'K' * 32}\nsite=https://org\n")
    m = pc.OWNER_MARK
    record = pc.json.dumps({"slug": "zab-test", "repo": "zab-test"})
    runner = FakeRunner(owners={ip: f"{m}JSON\n{record}\n\n{m}HOST\nws-zab-test\n{m}END\n"})
    monkeypatch.setattr(pc, "subprocess_runner", runner)
    wheel = tmp_path / "zulip_agent_bridge-0.2.2-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    argv = ["--roster", str(roster), "--out-dir", str(out), "--wheel", str(wheel)]
    code = pc.main([*argv, "--only", "zab-test", "--claude-steer"])
    stdout = capsys.readouterr().out
    assert code == 0, stdout
    assert "claude steer: ON" in stdout
    assert any(c[0] == "scp" and c[-1].startswith(f"ubuntu@{ip}:") for c in runner.calls)
    assert pc.SERVICE_INSTALL_CMD in runner.remotes(ip)


# ------------------------------------------------------------------------- the sage wrapper (#482)


def sage_rollout(out_dir, runner) -> tuple[pc.VmResult, list[str]]:
    result = pc.rollout_vm(ALICE_ROW, pc.Options(tag="v0.2.6", out_dir=out_dir), runner)
    return result, runner.remotes(ALICE)


def test_the_sage_wrapper_goes_in_before_the_list_that_says_what_it_changes(out_dir):
    """#482: on the Oct 9 image's layout the script replaces the link (noted, once), and the
    list's SageMath entry then says Sage's interfaces work; both before the instructions."""
    installed = (
        "sage: installed the wrapper as /home/agent/.local/bin/sage, in place of its link to "
        "/home/agent/miniforge/envs/sage/bin/sage\n"
    )
    result, remotes = sage_rollout(out_dir, FakeRunner(answers={SAGE_STEP: installed}))
    assert result.ok and result.notes == [installed.strip()]
    sage = remotes.index(pc.sage_cmd(STAGING))
    assert remotes[sage + 1] == pc.tools_cmd(STAGING, ("sage-wrapper",))
    assert remotes[sage + 1].endswith(" --with sage-wrapper")
    assert "agent_instructions.py" in remotes[sage + 2]
    assert pc.SERVICE_INSTALL_CMD in remotes[:sage]  # after the release and its doctor
    # as agent, from the staging directory, every value quoted (CONTRIBUTING.md, rule 3)
    assert pc.sage_cmd(STAGING) == ac.remote_chain(
        ["sudo", "chmod", "0644", f"{STAGING}/sage_wrapper.py", f"{STAGING}/sage"],
        pc.as_agent("/usr/bin/python3", f"{STAGING}/sage_wrapper.py", "install", f"{STAGING}/sage"),
    )


@pytest.mark.parametrize(
    "answer",
    [
        "sage: skipped: no SageMath here (no /home/agent/.local/bin/sage, no …)\n",
        "sage: skipped: /home/agent/.local/bin/sage is not zab's wrapper, nor the image's link: "
        "left alone\n",
        "",  # a script that said nothing: never taken for a wrapper in place
    ],
)
def test_a_vm_left_alone_keeps_the_caveat_and_says_why(out_dir, answer):
    result, remotes = sage_rollout(out_dir, FakeRunner(answers={SAGE_STEP: answer}))
    assert result.ok and result.notes == [answer.strip() or "sage: the wrapper's step said nothing"]
    assert pc.tools_cmd(STAGING) in remotes and "--with" not in " ".join(remotes)


def test_a_failed_sage_step_is_reported_and_holds_nothing_up(out_dir):
    runner = FakeRunner(fail={SAGE_STEP: "sage: failed: [Errno 13] Permission denied"})
    result, remotes = sage_rollout(out_dir, runner)
    assert result.failures == ["sage wrapper: sage: failed: [Errno 13] Permission denied"]
    assert result.installed and result.doctor_ok and not result.notes
    assert pc.tools_cmd(STAGING) in remotes  # the list with the caveat
    assert any("agent_instructions.py" in r for r in remotes)
    assert remotes[-2] == pc.warm_on_cmd(STAGING)


def test_uninstall_leaves_sage_as_the_rollout_left_it(out_dir):
    """`!uninstall` (workshop/admin/README.md §6) stops the bridge; CLI use is unaffected, so Sage
    keeps the wrapper that the VM's list and the agents' instructions describe."""
    runner = FakeRunner()
    result = pc.rollout_vm(ALICE_ROW, pc.Options(out_dir=out_dir, uninstall=True), runner)
    assert result.ok and not any("sage" in r for r in runner.remotes(ALICE))
    assert not [c for c in runner.calls if c[0] == "scp"]


class OnTheVm(FakeRunner):
    """The sage step and the list's step run for real (`sage_wrapper.install` on a layout under
    tmp_path, `tools_list.install` with the flags the command carries), so that what the script
    says and what push_config reads of it can't drift apart."""

    def __init__(self, vm, dest: pathlib.Path, monkeypatch):
        super().__init__()
        self.vm, self.dest, self.monkeypatch = vm, dest, monkeypatch

    def __call__(self, argv, **kw):
        remote = argv[-1] if argv[0] == "ssh" else ""
        if SAGE_STEP in remote:
            self.calls.append(list(argv))
            return ac.RunResult(0, self.vm.install() + "\n", "")
        if "tools_list.py install " in remote:
            self.calls.append(list(argv))
            words = shlex.split(remote)
            features = frozenset(words[i + 1] for i, w in enumerate(words) if w == "--with")
            with self.monkeypatch.context() as m:  # every path the list requires is there
                m.setattr(tm.os.path, "exists", lambda path: True)
                done = tm.install(str(pc.TOOLS_LIST), str(self.dest), features)
            return ac.RunResult(0, done + "\n", "")
        return super().__call__(argv, **kw)


@pytest.mark.parametrize("layout", ["oct9", "new image", "no sage", "another file"])
def test_the_list_says_what_the_script_found(out_dir, tmp_path, monkeypatch, layout):
    (tmp_path / "vm").mkdir()
    vm, dest = Vm(tmp_path / "vm"), tmp_path / "tools.json"
    if layout == "oct9":
        vm.oct9()
    elif layout == "new image":
        vm.new_image()
    elif layout == "another file":
        vm.with_sage().path.write_text("#!/bin/sh\n")
    runner = OnTheVm(vm, dest, monkeypatch)
    for rollout in (1, 2):  # the second rollout finds what the first one left
        result, _ = sage_rollout(out_dir, runner)
        assert result.ok, result.failures
        sage = next(t for t in json.loads(dest.read_text())["tools"] if t["name"] == "SageMath")
        wrapped = layout in ("oct9", "new image")
        assert ("gap(), giac() and r() included" in sage["summary"]) is wrapped
        assert ("need `PATH=" in sage["summary"]) is not wrapped
        sage_notes = [n for n in result.notes if n.startswith("sage: ")]
        if layout == "oct9" and rollout == 1:
            said = f"sage: installed the wrapper as {vm.path}, in place of its link to {vm.program}"
            assert sage_notes == [ac.first_line(said)]  # cut for the table, these paths are long
        elif wrapped:
            assert sage_notes == []
        else:
            assert [n.split(":")[1] for n in sage_notes] == [" skipped"]
