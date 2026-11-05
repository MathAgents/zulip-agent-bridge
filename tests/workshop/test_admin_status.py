"""`workshop/admin/status.py` (workshop/admin/README.md §3) and `export.sh` (§7.2)."""

from __future__ import annotations

import csv
import json
import os
import pathlib
import pwd
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
from typing import ClassVar

import admin_common as ac
import idle_stop
import push_config
import pytest
import status as st

from zab.commands.status import read_status
from zab.core.store import Store

ROSTER = """github,first_name,last_name,email,repo,github_id,host,ip,zulip_user_id,notes
alice,Alice,Smith,alice@example.org,alice/thesis,1,ws-alice,203.0.113.5,101,
bob,Bob,Jones,bob@example.org,sandbox,2,ws-bob,203.0.113.6,102,
carol,Carol,Lee,carol@example.org,carol/x,3,ws-carol,203.0.113.7,103,
"""

PREFLIGHT = {
    "host": "ws-alice",
    "checks": [
        {"name": "codex login", "ok": True, "detail": "logged in"},
        {"name": "codex smoke", "ok": True, "detail": "answered"},
        {"name": "claude -p", "ok": False, "detail": "not logged in"},
        {"name": "git push", "ok": True, "detail": "ok"},
        {"name": "gp", "ok": True, "detail": "2.17.1"},
        {"name": "cypari2", "ok": True, "detail": "2.2.4"},
        {"name": "sage", "ok": False, "detail": "absent", "warning": True},
        {"name": "disk space", "ok": True, "detail": "80 GB free"},
    ],
}
DOCTOR = [
    {"name": "config", "ok": True, "detail": "/home/agent/.config/zab/config.toml"},
    {"name": "codex:version", "ok": True, "detail": "0.157.1", "warning": True},
    {
        "name": "claude account main",
        "ok": False,
        "detail": "not logged in: run `claude` and use /login",
    },
    {
        "name": "codex:sentinel",
        "ok": True,
        "detail": "zab-sentinel.service active, its mark is from this boot, 40 seconds old",
    },
]
STATUS = {
    "sessions": [
        {"id": "s1", "bot": "codex", "state": "idle", "topic": "t1"},
        {"id": "s2", "bot": "claude", "state": "running", "topic": "t2"},
        {"id": "s3", "bot": "codex", "state": "detached", "topic": "t3"},
    ],
    "running_turns": [{"session_id": "s2"}],
}


#: The LOAD section as an m7a.2xlarge prints it: /proc/loadavg, nproc, two meminfo lines, df -Pk /
LOAD = (
    "0.42 0.30 0.20 1/234 5678\n8\n"
    "MemTotal:       32212254 kB\nMemAvailable:   28991029 kB\n"
    "/dev/root      101430960 41587424 59827152      42% /\n"
)


def alice_stdout() -> str:
    return (
        f"{st.MARK}PREFLIGHT\n{json.dumps(PREFLIGHT)}\n\n"
        f"{st.MARK}DOCTOR\n{json.dumps(DOCTOR)}\n\n"
        f"{st.MARK}STATUS\n{json.dumps(STATUS)}\n\n"
        f"{st.MARK}SERVICE\nactive\n\n"
        f"{st.MARK}LOAD\n{LOAD}\n"
        f"{st.MARK}END\n"
    )


def bob_stdout() -> str:  # a VM without zab yet: empty sections
    names = ("PREFLIGHT", "DOCTOR", "STATUS", "SERVICE", "END")
    return "".join(f"{st.MARK}{name}\n\n" for name in names)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.calls.append(list(argv))
        host = argv[-3].split("@", 1)[1]
        if host == "203.0.113.5":
            return ac.RunResult(0, alice_stdout(), "")
        if host == "203.0.113.6":
            return ac.RunResult(0, bob_stdout(), "")
        return ac.RunResult(255, "", "ssh: connect to host 203.0.113.7 port 22: No route to host")


def test_split_sections():
    s = st.split_sections(alice_stdout())
    assert set(s) == {"PREFLIGHT", "DOCTOR", "STATUS", "SERVICE", "LOAD"}
    assert json.loads(s["PREFLIGHT"]) == PREFLIGHT and s["SERVICE"] == "active"


def test_summarize_alice():
    row = st.summarize({"github": "alice", "host": "ws-alice"}, alice_stdout())
    assert row.row() == [
        "alice",
        "ws-alice",
        "ok",
        "no",  # the doctor: `claude account main` not signed in yet (PM on #36, Wed 13:35)
        "ok",
        "ok",
        "active, sign-in pending",  # only a sign-in check fails (PM on #36, Mon 13:45)
        "ok",  # the sentinel (#588)
        "2 (1 running)",
        "0.4/8",
        "3/31G",
        "42%",
        "preflight claude -p: not logged in",
    ]


def doctor_row(checks: list[dict]) -> st.VmStatus:
    out = f"{st.MARK}DOCTOR\n{json.dumps(checks)}\n\n{st.MARK}SERVICE\nactive\n\n{st.MARK}END\n"
    return st.summarize({"slug": "alice", "host": "h"}, out)


def test_the_sentinel_column_is_the_doctors_sentinel_checks():
    """#588: `ok` when no `<bot>:sentinel` check warns, else the states of those that do (the
    first words of their detail); `?` without one, as from a zab before #588."""
    from zab.tools import sentinel

    assert st.SENTINEL_STATES == sentinel.UNHEALTHY  # the copy

    def check(bot: str, detail: str, warning: bool = True) -> dict:
        tail = "; DM !restart is unavailable, and the bridge answers DM !vm itself" * warning
        return {"name": f"{bot}:sentinel", "ok": True, "warning": warning, "detail": detail + tail}

    ok = check("codex", "zab-sentinel.service active, its mark is from this boot", False)
    assert st.sentinel_state(None) == st.sentinel_state([]) == "?"
    assert st.sentinel_state([{"name": "codex:login", "ok": True}, "junk", 3]) == "?"
    assert st.sentinel_state([ok]) == "ok"
    assert st.sentinel_state([ok, check("claude", "stale: its mark is from another boot")]) == (
        "stale"
    )
    down = check("a", "down: zab-sentinel.service is failed")
    assert st.sentinel_state([down, down, check("b", "no mark: no Zulip queue")]) == "down, no mark"
    assert st.sentinel_state([check("a", "a state of a later zab: why")]) == "warn"
    unknown = check("a", "unknown: can't reach the user's systemd: no answer", False)
    assert st.sentinel_state([{**unknown, "warning": True}]) == "unknown"
    row = doctor_row([down])  # a warning, never a doctor failure
    assert row.sentinel == "down" and row.bridge == "active" and not row.doctor_failures
    assert st.vm_json(row)["sentinel"] == "down"
    assert st.vm_json(doctor_row([]))["sentinel"] is None


def test_only_sign_in_failures_read_as_pending_like_push_config():
    """PM on #36 (Mon 13:45): status.py shows the VMs nobody has signed in to yet the way
    push_config does (#152): `active, sign-in pending`, not `active, doctor FAIL`."""
    login = [
        {"name": "codex:login", "ok": False, "detail": ""},
        {"name": "claude account main", "ok": False, "detail": "not signed in: send !login"},
    ]
    row = doctor_row(login)
    assert row.bridge == "active, sign-in pending" and row.last_error == ""
    # a sign-in failure plus another one is a failure, listed first
    row = doctor_row([{"name": "zulip:claude", "ok": False, "detail": "401"}, *login])
    assert row.bridge == "active, doctor FAIL" and row.last_error == "zulip:claude: 401"
    # an operational Claude failure has its own name and is never pending (codex on #152)
    for name in ("claude config main", "claude auth main"):
        row = doctor_row([{"name": name, "ok": False, "detail": "broken"}])
        assert row.bridge == "active, doctor FAIL"
    assert doctor_row([{"name": "config", "ok": True}]).bridge == "active"


def test_an_unbound_rows_allowlist_reads_as_pending_like_push_config():
    """#401: before the participant joins Zulip, the row has no `zulip_user_id` and the bots'
    allowlist checks fail (an empty detail before #401); that is pending on an unbound row, a
    failure on a bound one."""
    login = [
        {"name": "codex:login", "ok": False, "detail": ""},
        {"name": "claude account main", "ok": False, "detail": "not signed in: send !login"},
    ]
    waiting = (
        "no allowed user yet: no Zulip user with the allowed e-mail visible to this bot "
        "(not joined yet, or the address is hidden from members)"
    )
    out = f"{st.MARK}SERVICE\nactive\n\n{st.MARK}END\n"
    unbound = {"slug": "alice", "host": "h", "email": "alice@example.org", "zulip_user_id": ""}
    for detail in (waiting, ""):
        allow = [
            {"name": f"{b}:allowlist", "ok": False, "detail": detail} for b in ("codex", "claude")
        ]

        def vm(checks, row=unbound):
            return st.summarize(row, f"{st.MARK}DOCTOR\n{json.dumps(checks)}\n\n{out}")

        row = vm([*login, *allow])
        assert row.bridge == "active, owner+sign-in pending" and row.last_error == ""
        assert vm(allow).bridge == "active, owner pending"
        row = vm([*allow, *login], {**unbound, "zulip_user_id": "101"})
        assert row.bridge == "active, doctor FAIL"
        assert row.doctor_failures[0] == f"codex:allowlist: {detail}".strip()
        row = vm([*allow, {"name": "zulip:claude", "ok": False, "detail": "401"}])
        assert row.bridge == "active, doctor FAIL" and row.last_error == "zulip:claude: 401"
    nobody = {"name": "codex:allowlist", "ok": False, "detail": "nobody allowed: …"}
    assert vm([*login, nobody]).bridge == "active, doctor FAIL"


def test_the_sign_in_rule_is_shared():
    assert ac.sign_in_pending([{"name": "codex:login"}, {"name": "claude account alt"}])
    assert not ac.sign_in_pending([])
    assert not ac.sign_in_pending([{"name": "codex:login"}, {"name": "claude auth main"}])


def test_codex_login_reflects_the_smoke_request():
    """`codex login status` can pass with a stale token; the smoke request decides (round 3)."""
    pre = {
        "checks": [
            {"name": "codex login", "ok": True, "detail": "logged in"},
            {"name": "codex smoke", "ok": False, "detail": "401 Unauthorized"},
            {"name": "claude smoke", "ok": True, "detail": "answered"},
        ]
    }
    out = f"{st.MARK}PREFLIGHT\n{json.dumps(pre)}\n{st.MARK}END\n"
    row = st.summarize({"github": "x", "host": "h"}, out)
    assert row.codex_login == "FAIL" and row.claude_login == "ok"
    assert row.last_error == "preflight codex smoke: 401 Unauthorized"


def test_summarize_without_zab():
    row = st.summarize({"github": "bob", "host": "ws-bob"}, bob_stdout())
    assert row.codex_login == "?" and row.bridge == "no zab" and row.sessions == "?"
    assert row.last_error == ""


def test_gather_unreachable_and_main_csv(tmp_path, monkeypatch, capsys):
    runner = FakeRunner()
    monkeypatch.setattr(st, "subprocess_runner", runner)
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)
    out_csv = tmp_path / "status.csv"
    code = st.main(
        [
            "--roster",
            str(roster),
            "--csv",
            str(out_csv),
            "--parallel",
            "3",
            "--known-hosts",
            str(tmp_path / "kh"),
        ]
    )
    assert code == 0
    text = capsys.readouterr().out
    lines = [line for line in text.splitlines() if line.startswith(("alice", "bob", "carol"))]
    assert len(lines) == 3 and "unreachable" in lines[2] and "No route to host" in lines[2]
    with open(out_csv, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == st.COLUMNS and rows[1][0] == "alice" and rows[3][6] == "unreachable"
    # one fixed request per host (the roster's ip, host key pinned), as the read user (#507);
    # carol's, unreachable, is not tried again as ubuntu
    assert len(runner.calls) == 3
    for c in runner.calls:
        assert c[0] == "ssh" and c[-1] == st.READ_REQUEST and c[-3].startswith("zab-monitor@203.")
        assert f'UserKnownHostsFile="{tmp_path / "kh"}"' in c and "StrictHostKeyChecking=yes" in c
    assert not any("ws-" in part for c in runner.calls for part in c)
    # a roster with two rows on one IP is refused before any connection
    roster.write_text(ROSTER + "dave,D,K,d@example.org,d/x,4,ws-dave,203.0.113.5,104,\n")
    runner.calls.clear()
    assert st.main(["--roster", str(roster)]) == 1 and not runner.calls
    assert "roster: duplicate ip" in capsys.readouterr().err
    assert "sudo -u agent -H XDG_RUNTIME_DIR=/run/user/$uid" in st.STATUS_CMD
    assert 'bash -lc "cd / && $1"' in st.STATUS_CMD  # probes start from / (round 3)
    # every probe, the preflight read included, runs as agent: ubuntu cannot traverse its home
    assert "run 'cat ~/.local/state/zab/preflight.json'" in st.STATUS_CMD
    assert "cat /home/agent" not in st.STATUS_CMD
    # the doctor push_config runs (PM on #36, Wed 13:00): service:python and service:gp too
    assert "run 'zab doctor --service-env --json'" in st.STATUS_CMD
    assert "zab status --json" in st.STATUS_CMD
    assert text.rstrip().splitlines()[-1].startswith("signed in: ")


def test_the_read_user_first_then_ubuntu_where_it_is_refused():
    """#507: as zab-monitor, which idle stop doesn't count; as ubuntu (STATUS_CMD, with the
    agent's runtime directory) only where that user is refused or its read fails on the VM,
    never after a timeout or a failed connection, which would only wait as long again."""
    first = {
        "203.0.113.5": ac.RunResult(0, alice_stdout(), ""),
        "203.0.113.6": ac.RunResult(255, "", "zab-monitor@x: Permission denied (publickey)."),
        "203.0.113.7": ac.RunResult(1, "", "sudo: a password is required"),
        "203.0.113.8": ac.RunResult(255, "", "ssh: connect to host x port 22: No route to host"),
        "203.0.113.9": ac.RunResult(-1, "", "timed out after 180 s: ssh"),
    }
    calls = []

    def runner(argv, *, timeout_s=120.0, env=None, stdin=None):
        user, host = argv[-3].split("@", 1)
        calls.append((user, host, argv[-1]))
        return ac.RunResult(0, alice_stdout(), "") if user == "ubuntu" else first[host]

    rows = [st.gather_vm({"github": ip[-1], "ip": ip}, runner) for ip in first]
    second = [(user, host) for user, host, cmd in calls if cmd == st.STATUS_CMD]
    assert second == [("ubuntu", "203.0.113.6"), ("ubuntu", "203.0.113.7")]
    assert [c[2] for c in calls if c[0] == "zab-monitor"] == [st.READ_REQUEST] * 5
    assert [r.bridge for r in rows][3:] == ["unreachable"] * 2
    assert all(r.bridge.startswith("active") for r in rows[:3])


def test_the_read_script_is_the_same_read_as_agent(tmp_path):
    """The script zab-monitor's forced command runs as agent: STATUS_CMD's sections, without
    sudo, refused under any other user."""
    script = st.READ_SCRIPT
    assert script.startswith("#!/bin/bash\n") and script.endswith(st._SECTIONS + "\n")
    assert st.STATUS_CMD.endswith(st._SECTIONS) and "sudo" not in script
    (tmp_path / "bin").mkdir()  # a fake zab, and a HOME of its own: no real zab runs
    (tmp_path / "bin" / "zab").write_text('#!/bin/sh\necho "fake zab $*"\n')
    (tmp_path / "bin" / "zab").chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": f"{tmp_path}/bin:/usr/bin:/bin"}
    path, bash = tmp_path / "read.sh", shutil.which("bash") or "/bin/bash"
    path.write_text(script)
    run = [bash, str(path)]
    res = subprocess.run(run, capture_output=True, text=True, timeout=60, env=env)  # noqa: S603
    assert res.returncode == 2 and "run it as agent" in res.stderr
    path.write_text(script.replace("= agent ]", f'= "{pwd.getpwuid(os.getuid()).pw_name}" ]'))
    res = subprocess.run(run, capture_output=True, text=True, timeout=60, env=env)  # noqa: S603
    sections = st.split_sections(res.stdout)
    assert set(sections) == {"PREFLIGHT", "DOCTOR", "STATUS", "VERSION", "SERVICE", "IDLE", "LOAD"}
    assert sections["DOCTOR"] == "fake zab doctor --service-env --json"


# ------------------------------------- columns without preflight.json (PM on #36, Wed 13:00)

FLEET_DOCTOR = [
    {"name": "config", "ok": True, "detail": "/home/agent/.config/zab/config.toml"},
    {"name": "codex:connection", "ok": True, "detail": "/home/agent/.codex"},
    {"name": "codex:login", "ok": False, "detail": "not signed in: send !login to this bot"},
    {"name": "claude account main", "ok": False, "detail": "not signed in: send !login"},
    {"name": "service:gp", "ok": True, "detail": "not found in service PATH (informational)"},
    {"name": "service:python", "ok": True, "detail": "/home/agent/venv/bin/python3: OK"},
]


def fleet_row(doctor, *, preflight=None, **row):
    """A VM as on Wednesday's fleet: no PREFLIGHT section unless given, the bridge active."""
    out = f"{st.MARK}PREFLIGHT\n{json.dumps(preflight) if preflight else ''}\n\n"
    out += f"{st.MARK}DOCTOR\n{json.dumps(doctor)}\n\n{st.MARK}SERVICE\nactive\n\n"
    out += f"{st.MARK}END\n"
    return st.summarize({"slug": "alice", "host": "h", **row}, out)


def test_the_columns_come_from_the_doctor_and_the_roster_without_preflight():
    row = fleet_row(FLEET_DOCTOR)
    # codex login, claude login, git push, math tools: nobody signed in yet reads `no`
    assert row.row()[2:6] == ["no", "no", "?", "FAIL"]
    assert row.bridge == "active, sign-in pending"  # unchanged: only sign-in checks fail
    # provision_repos.py's repo_status stands in for the push test: ok, or anything else
    assert fleet_row(FLEET_DOCTOR, repo_status="ok").git_push == "ok"
    assert fleet_row(FLEET_DOCTOR, repo_status="failed: push").git_push == "FAIL"
    # gp on the service PATH and numpy/cypari2 importing: math tools ok
    found = [*FLEET_DOCTOR[:4], {"name": "service:gp", "ok": True, "detail": "/usr/local/bin/gp"}]
    assert fleet_row([*found, FLEET_DOCTOR[5]]).math_tools == "ok"
    broken = {"name": "service:python", "ok": False, "detail": "imports failed"}
    assert fleet_row([*found, broken]).math_tools == "FAIL"
    # no service:python (a config without a service environment): gp alone proves nothing, so
    # preflight's answer, or `?`, stands; gp missing is still a failure (codex on #186)
    cypari2 = {"checks": [{"name": "cypari2", "ok": False, "detail": "ImportError"}]}
    assert fleet_row(found, preflight=cypari2).math_tools == "FAIL"
    assert fleet_row(found).math_tools == "?"
    assert fleet_row(FLEET_DOCTOR[:5]).math_tools == "FAIL"  # gp not found, no python check
    # a real failure next to a sign-in still pending: FAIL wins over `no`
    auth = {"name": "claude auth alt", "ok": False, "detail": "auth status failed"}
    assert fleet_row([*FLEET_DOCTOR, auth]).claude_login == "FAIL"


def test_the_live_doctor_wins_over_a_stale_preflight():
    stale = {
        "checks": [
            {"name": "codex login", "ok": True, "detail": "logged in"},
            {"name": "codex smoke", "ok": True, "detail": "answered"},
            {"name": "git push", "ok": False, "detail": "denied"},
        ]
    }
    row = fleet_row(FLEET_DOCTOR, preflight=stale, repo_status="ok")
    assert row.codex_login == "no"  # signed out since the preflight ran
    assert row.git_push == "FAIL"  # a real push test comes before the roster


def test_not_signed_in_reads_no_and_real_errors_fail():
    """Drew via the PM (issue #36, Wed 13:35): before `!login` the logins read `no`, not an
    alarming `FAIL`; `FAIL` stays for real errors (`admin_common.sign_in_check` decides)."""

    def logins(*checks):
        row = fleet_row([{"name": "config", "ok": True}, *checks])
        return row.codex_login, row.claude_login

    signed_in = (
        {"name": "codex:connection", "ok": True, "detail": "~/.codex"},
        {"name": "codex:login", "ok": True, "detail": "signed in"},
        {"name": "claude account main", "ok": True, "detail": "signed in"},
    )
    assert logins(*signed_in) == ("ok", "ok")
    for name in ("claude auth main", "claude config main"):  # operational, never `no`
        assert logins({"name": name, "ok": False, "detail": "broken"})[1] == "FAIL"
    pending = {"name": "claude account a", "ok": False, "detail": "not signed in: send !login"}
    broken = {"name": "claude auth b", "ok": False, "detail": "auth status failed"}
    assert logins(pending)[1] == "no" and logins(pending, broken)[1] == "FAIL"
    # the summary still counts `ok` against anything else
    assert st.summary_line([fleet_row(FLEET_DOCTOR)]).startswith("signed in: 0 both, 0 Codex")


def test_a_daemon_down_without_codex_login_is_a_codex_fail():
    down = [{"name": "codex:connection", "ok": False, "detail": "daemon unavailable"}]
    assert fleet_row(down).codex_login == "FAIL"
    assert fleet_row([{"name": "config", "ok": True}]).codex_login == "?"


def test_the_summary_line_counts_sign_ins_and_unreachable_rows():
    def vm(codex, claude, bridge="active, sign-in pending"):
        return st.VmStatus(slug="x", codex_login=codex, claude_login=claude, bridge=bridge)

    rows = [
        vm("ok", "ok", "active"),
        vm("ok", "ok", "active, doctor FAIL"),
        vm("ok", "FAIL"),
        vm("?", "ok"),
        vm("FAIL", "?"),
        vm("?", "?", "unreachable"),
        vm("?", "?", "no zab"),
    ]
    assert st.summary_line(rows) == (
        "signed in: 2 both, 1 Codex only, 1 Claude only, 1 neither; 2 unreachable, 0 stopped, "
        "1 doctor FAIL"
    )


def test_load_mem_and_disk_columns_and_the_strain_marks():
    assert st.load_columns(LOAD) == {"load": "0.4/8", "mem": "3/31G", "disk": "42%"}
    strained = (
        "9.70 8.10 6.00 12/400 9999\n8\n"
        "MemTotal:       32212254 kB\nMemAvailable:    2000000 kB\n"
        "/dev/root      101430960 95000000 6430960      94% /\n"
    )
    # a disk under strain names df's available space, in binary GB (#552): 6.13 GiB
    assert st.load_columns(strained) == {
        "load": "9.7/8 !",
        "mem": "29/31G !",
        "disk": "94% ! (6.2 GB free)",
    }
    # a missing or garbled part stays `?`, the others still read
    assert st.load_columns("8\nMemTotal: x kB\n/dev/root 1 2 3 7% /\n") == {"disk": "7%"}
    assert st.load_columns("/dev/root 1 2 x 97% /\n") == {"disk": "97% !"}  # no sizes: as before
    assert st.load_columns("") == {}
    rows = [
        st.VmStatus(slug="alice", load="0.4/8", mem="3/31G", disk="42%"),
        st.VmStatus(slug="bob", load="9.7/8 !", mem="3/31G", disk="92% ! (15 GB free)"),
        st.VmStatus(slug="carol", mem="29/31G !", disk="94% !"),
        st.VmStatus(slug="dave"),  # unreachable: all `?`
    ]
    assert st.strain_line(rows) == "under strain: bob (load, disk), carol (mem, disk)"
    assert st.strain_line(rows[:1] + rows[3:]) is None


def test_the_disk_sizes_come_from_the_same_df_line_in_kib():
    """#552: `df -Pk /`'s data line already holds the size, Used and Available (1024-blocks)
    next to the percentage; the status read sends it whole (`tail -n 1`), so the VM's side is
    unchanged. Each is kept only when consistent, and the percentage without them."""
    assert st.load_numbers(LOAD) == {
        "load1": 0.42,
        "cpus": 8,
        "mem_total_kib": 32212254,
        "mem_available_kib": 28991029,
        "disk_percent": 42,
        "disk_size_kib": 101430960,
        "disk_used_kib": 41587424,
        "disk_available_kib": 59827152,
    }
    sizes = ("disk_size_kib", "disk_used_kib", "disk_available_kib")
    for line in (
        "/dev/root 0 0 0 0% /",  # no size
        "/dev/root 100 101 0 100% /",  # more used than the size
        "/dev/root 100 1 101 1% /",  # more available than the size
        "/dev/root 100 -1 50 1% /",
        "/dev/root 100 1 1.5 1% /",
    ):
        found = st.load_numbers(line)
        assert "disk_percent" in found and not any(k in found for k in sizes), line
    two = st.load_numbers("/dev/root 100 10 80 12% /\n/dev/x 9 9 0 100% /\n")
    assert (two["disk_percent"], two["disk_used_kib"]) == (12, 10)  # the first line, as before
    # the LOAD section is a fixed literal: the read is the one VMs already run (no change)
    section = st.STATUS_CMD.split(f"echo {st.MARK}LOAD;", 1)[1].split(f"echo {st.MARK}END")[0]
    assert "df -Pk / | tail -n 1; echo; " in section and section in st.READ_SCRIPT


def test_disk_gb_is_binary_rounded_up_as_df_dash_h_and_the_console_agrees():
    """Binary GB, as memory and `!vm` (#204), rounded up, to a tenth below 10 GB, as `df -h`
    rounds them (#552); `zab.admin.pages` has the same function, which the console and
    `!status` use. The last cases are the development machine's `df -B1` and `df -h` (or
    `df -B G`)."""
    from zab.admin import pages

    gib, tenth = 2**30, 2**30 // 10  # 107374182, a tenth rounded down
    cases = {0: "0", 1: "0.1", tenth: "0.1", tenth + 1: "0.2", 6_585_303_040: "6.2",
             99 * gib // 10: "9.9", 99 * gib // 10 + 1: "10", 10 * gib: "10", 15 * gib: "15",
             15 * gib + 1: "16", 203_056_560 * 1024: "194",  # the fleet's root, 193.6 GiB
             3_927_306_629_120: "3658", 4_286_562_304: "4.0", 3_057_242_112: "2.9",
             1_229_320_192: "1.2"}  # fmt: skip
    for n, shown in cases.items():
        assert st.disk_gb(n) == pages.disk_gb(n) == shown, n
    for n in [*range(0, 3 * gib // 10, 9_999_991), *range(9 * gib, 11 * gib, 99_999_989)]:
        assert st.disk_gb(n) == pages.disk_gb(n), n


def test_the_load_section_is_read_as_ubuntu_from_proc_and_df():
    section = st.STATUS_CMD.split(f"echo {st.MARK}LOAD;", 1)[1].split(f"echo {st.MARK}END")[0]
    assert "cat /proc/loadavg" in section and "nproc" in section and "df -Pk /" in section
    assert "/proc/meminfo" in section and "run " not in section and "sudo" not in section
    assert st.COLUMNS[-4:] == ["load", "mem", "disk", "last error"]


# ------------------------------------------------------------------------------- export.sh

EXPORT = pathlib.Path(__file__).resolve().parents[2] / "workshop" / "admin" / "export.sh"
BASH = shutil.which("bash") or "/bin/bash"


def test_export_script_syntax_and_excludes():
    subprocess.run([BASH, "-n", str(EXPORT)], check=True)  # noqa: S603 - fixed argv
    text = EXPORT.read_text()
    for excluded in (".codex/auth.json", ".claude/.credentials.json", ".config/zab"):
        assert f"--exclude='{excluded}'" in text
    assert "set -euo pipefail" in text


def test_export_script_archives_work_and_excludes_credentials(tmp_path):
    home = tmp_path / "home"
    (home / "work" / "repo").mkdir(parents=True)
    (home / "work" / "repo" / "notes.md").write_text("notes")
    lake = home / "work" / "repo" / ".zab" / "worktrees" / "t-1a2b3c" / "proof" / ".lake"
    (lake / "build").mkdir(parents=True)
    (lake / "build" / "Mathlib.olean").write_text("x")
    (lake.parent / "Main.lean").write_text("theorem t : True := trivial")
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".codex" / "sessions" / "r.jsonl").write_text("{}")
    (home / ".codex" / "auth.json").write_text('{"token": "decoy"}')
    (home / ".claude" / "projects" / "p").mkdir(parents=True)
    (home / ".claude" / "projects" / "p" / "s.jsonl").write_text("{}")
    (home / ".claude" / ".credentials.json").write_text('{"decoy": 1}')
    (home / ".codex" / "AGENTS.md").write_text("managed block, then my own notes\n")
    (home / ".claude" / "CLAUDE.md").write_text("@~/.codex/AGENTS.md\n")
    (home / ".config" / "zab").mkdir(parents=True)
    (home / ".config" / "zab" / "codex.zuliprc").write_text("[api]\nkey=decoy\n")
    participant = tmp_path / "participant.json"
    participant.write_text(json.dumps({"github": "alice", "repo": "thesis"}))
    env = {**os.environ, "HOME": str(home), "ZAB_PARTICIPANT_JSON": str(participant)}
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [BASH, str(EXPORT)], env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    archive = home / "export-alice.tar.gz"
    assert archive.exists() and "export-alice.tar.gz" in proc.stdout
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "work/repo/notes.md" in names and ".codex/sessions/r.jsonl" in names
    assert ".claude/projects/p/s.jsonl" in names
    # a topic's Lean sources come along, its .lake (a whole Mathlib) does not (Sat Oct 10)
    assert "work/repo/.zab/worktrees/t-1a2b3c/proof/Main.lean" in names
    assert not any("/.lake" in n for n in names)
    # the participant keeps their personal section (PM on issue #36, Mon 19:00)
    assert ".codex/AGENTS.md" in names and ".claude/CLAUDE.md" in names
    assert not any(n.endswith(("auth.json", ".credentials.json", ".zuliprc")) for n in names)
    assert not any(n.startswith(".config/zab") for n in names)


def test_export_script_rejects_bad_login(tmp_path):
    participant = tmp_path / "participant.json"
    participant.write_text(json.dumps({"github": "evil; rm -rf /", "repo": "x"}))
    env = {**os.environ, "HOME": str(tmp_path), "ZAB_PARTICIPANT_JSON": str(participant)}
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [BASH, str(EXPORT)], env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 1 and "bad slug" in proc.stderr
    assert not list(tmp_path.glob("export-*.tar.gz"))


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([{"name": "gp", "ok": True}, {"name": "cypari2", "ok": True}], "ok"),
        ([{"name": "gp", "ok": True}, {"name": "cypari2", "ok": False}], "FAIL"),
        ([{"name": "disk space", "ok": True}], "?"),
    ],
)
def test_check_helper(checks, expected):
    assert st._check(checks, "gp", "cypari2") == expected


# ------------------------------------------------------ stopped kept VMs (issue #207)


class FakeEc2:
    """`find_instances` by region, with each instance's Elastic IP."""

    regions: ClassVar[list[str]] = []
    fail = False

    def __init__(self, region):
        if FakeEc2.fail:
            raise ImportError("No module named 'boto3'")
        FakeEc2.regions.append(region)
        self.region = region

    def find_instances(self, tags, states):
        assert tags == {"workshop": "nt-agents-2026"} and "stopped" in states
        fleet = {
            "us-west-2": [
                ("carol", "stopped", "203.0.113.7"),
                ("dave", "running", "203.0.113.8"),
                ("frank", "stopped", "203.0.113.20"),  # frank's broken original, handed over
                ("zab-spare-1", "running", "203.0.113.30"),  # frank's VM now
            ],
            "us-east-2": [("erin", "pending", "203.0.113.9")],
        }
        return [
            {
                "State": {"Name": state},
                "Tags": [{"Key": "participant", "Value": who}],
                "NetworkInterfaces": [{"Association": {"PublicIp": ip}}],
                **({"PublicIpAddress": ip} if state == "running" else {}),
            }
            for who, state, ip in fleet.get(self.region, [])
        ]


@pytest.fixture
def fake_ec2(monkeypatch):
    FakeEc2.regions = []
    FakeEc2.fail = False
    monkeypatch.setattr(st, "Ec2", FakeEc2)
    return FakeEc2


def test_unreachable_vms_that_ec2_says_are_stopped_say_so(fake_ec2):
    rows = [
        {"slug": "carol", "region": "", "ip": "203.0.113.7"},
        {"slug": "dave", "region": "us-west-2", "ip": "203.0.113.8"},
        {"slug": "erin", "region": "us-east-2", "ip": "203.0.113.9"},
        {"slug": "gina", "region": "us-west-2", "ip": "203.0.113.10"},
    ]
    results = [
        st.VmStatus(slug=r["slug"], bridge="unreachable", last_error="timeout") for r in rows
    ]
    states, problem = st.ec2_states({st.region_of(r) for r in rows})
    assert not problem and sorted(fake_ec2.regions) == ["us-east-2", "us-west-2"]
    st.mark_stopped(results, rows, states)
    assert [(r.bridge, r.last_error) for r in results] == [
        ("stopped", ""),  # the region defaults to us-west-2; the stopped VM keeps its EIP
        ("unreachable", "timeout"),  # running but unreachable: a real problem
        ("starting", ""),
        ("unreachable", "timeout"),  # no instance at all
    ]
    assert st.summary_line(results).endswith("2 unreachable, 2 stopped, 0 doctor FAIL")


def test_a_handed_over_vm_is_matched_by_address_not_by_name(fake_ec2):
    """Codex on #220: frank's roster ip is the spare's; his stopped original, still tagged with
    his name, must not hide that the spare he uses now is unreachable."""
    rows = [{"slug": "frank", "region": "us-west-2", "ip": "203.0.113.30"}]
    results = [st.VmStatus(slug="frank", bridge="unreachable", last_error="timeout")]
    states, _ = st.ec2_states({"us-west-2"})
    st.mark_stopped(results, rows, states)
    assert (results[0].bridge, results[0].last_error) == ("unreachable", "timeout")


def test_a_failed_ec2_lookup_says_why_and_changes_nothing(fake_ec2):
    fake_ec2.fail = True
    states, problem = st.ec2_states({"us-west-2"})
    assert states == {} and problem == "ImportError: No module named 'boto3'"
    states, problem = st.ec2_states({"us-east-2", "us-west-2"})  # one reason for all: alone
    assert states == {} and problem == "ImportError: No module named 'boto3'"


def test_a_region_that_fails_is_said_and_the_others_are_read(monkeypatch, fake_ec2):
    """Review of #453: the regions after a failed one are still read, and the reason names
    the regions it holds for."""
    found = FakeEc2.find_instances

    def find_instances(self, tags, states):
        if self.region in ("ap-south-1", "eu-west-1"):
            raise RuntimeError(f"UnauthorizedOperation in {self.region}")
        return found(self, tags, states)

    monkeypatch.setattr(fake_ec2, "find_instances", find_instances)
    states, problem = st.ec2_states({"ap-south-1", "eu-west-1", "us-east-2", "us-west-2"})
    assert states["203.0.113.7"] == "stopped" and states["203.0.113.9"] == "pending"
    assert problem == (
        "RuntimeError: UnauthorizedOperation in ap-south-1 (in ap-south-1); "
        "RuntimeError: UnauthorizedOperation in eu-west-1 (in eu-west-1)"
    )


def test_the_first_lookup_reads_the_regions_of_rows_with_a_vm(
    tmp_path, monkeypatch, capsys, fake_ec2
):
    """Review of #453: a row with no `ip` yet (before its launch, a late joiner before
    `!adopt-spare`) matches no instance, so its region (us-west-2 when empty) isn't read."""
    runner = FakeRunner()
    monkeypatch.setattr(st, "subprocess_runner", runner)
    roster = tmp_path / "roster.csv"
    head = "slug,first_name,last_name,email,region,ip"
    roster.write_text(f"{head}\nerin,E,L,e@example.org,us-east-2,203.0.113.9\nlate,L,T,l@x.org,,\n")
    assert st.main(["--roster", str(roster), "--known-hosts", str(tmp_path / "kh"), "--json"]) == 0
    captured = capsys.readouterr()
    vms = {vm["slug"]: vm for vm in json.loads(captured.out)["vms"]}
    assert vms["erin"]["bridge"] == "starting" and vms["late"]["bridge"] is None
    assert fake_ec2.regions == ["us-east-2"] and not runner.calls and not captured.err


def ssh_hosts(runner: FakeRunner) -> list[str]:
    return sorted(c[-3].split("@", 1)[1] for c in runner.calls)


def test_main_reads_ec2_first_and_never_tries_ssh_on_a_stopped_vm(
    tmp_path, monkeypatch, capsys, fake_ec2
):
    """#439: EC2 first, once per region; carol, stopped there, is reported from that state with
    no SSH attempt (which would wait out ConnectTimeout, or a shutdown). Nothing is unreachable,
    so there is no second lookup."""
    runner = FakeRunner()
    monkeypatch.setattr(st, "subprocess_runner", runner)
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER)  # carol, at 203.0.113.7, is stopped (and SSH can't reach her)
    assert st.main(["--roster", str(roster), "--known-hosts", str(tmp_path / "kh")]) == 0
    captured = capsys.readouterr()
    carol = next(line for line in captured.out.splitlines() if line.startswith("carol"))
    assert "stopped" in carol and "No route to host" not in carol and "203.0.113.7" in carol
    assert ssh_hosts(runner) == ["203.0.113.5", "203.0.113.6"]  # alice and bob only
    assert fake_ec2.regions == ["us-west-2"] and not captured.err
    assert "1 unreachable, 1 stopped" in captured.out  # bob's VM has no zab yet: unreachable
    # --no-ec2, or a failed lookup: every VM is tried over SSH, and carol stays unreachable
    for flags in (["--no-ec2"], []):
        runner.calls.clear()
        fake_ec2.regions.clear()
        fake_ec2.fail = not flags
        st.main(["--roster", str(roster), "--known-hosts", str(tmp_path / "kh"), *flags])
        captured = capsys.readouterr()
        assert "2 unreachable, 0 stopped" in captured.out
        assert ssh_hosts(runner) == ["203.0.113.5", "203.0.113.6", "203.0.113.7"]
        assert not fake_ec2.regions  # none read; a failed lookup is not tried again
        assert ("EC2 lookup failed (ImportError" in captured.err) is not bool(flags)


def test_a_failed_lookup_is_said_only_when_a_vm_is_unreachable(
    tmp_path, monkeypatch, capsys, fake_ec2
):
    """Without boto3 every VM is tried over SSH; a fleet that answers needs no EC2 state, so no
    line says the lookup failed, and `ec2_error` is null, as when the lookup came after SSH."""
    monkeypatch.setattr(st, "subprocess_runner", FakeRunner())
    roster = tmp_path / "roster.csv"
    roster.write_text(ROSTER.rsplit("carol", 1)[0])  # alice and bob, both reachable
    fake_ec2.fail = True
    argv = ["--roster", str(roster), "--known-hosts", str(tmp_path / "kh")]
    assert st.main(argv) == 0 and not capsys.readouterr().err
    assert st.main([*argv, "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ec2_error"] is None and not captured.err


def test_a_vm_that_stops_while_ssh_tries_is_looked_up_again(
    tmp_path, monkeypatch, capsys, fake_ec2
):
    """dave runs at the first lookup, then stops: SSH can't reach him, and the second lookup,
    for the unreachable rows alone, says why."""
    calls = []

    def find_instances(self, tags, states):
        calls.append(self.region)
        state = "running" if len(calls) == 1 else "stopping"
        return [{"State": {"Name": state}, "PublicIpAddress": "203.0.113.8"}]

    monkeypatch.setattr(fake_ec2, "find_instances", find_instances)
    runner = FakeRunner()  # 203.0.113.8 is not a host it answers: No route to host
    monkeypatch.setattr(st, "subprocess_runner", runner)
    roster = tmp_path / "roster.csv"
    head = ROSTER.split("\n", 1)[0]
    roster.write_text(f"{head}\ndave,D,K,d@example.org,d/x,4,ws-dave,203.0.113.8,104,\n")
    assert st.main(["--roster", str(roster), "--known-hosts", str(tmp_path / "kh"), "--json"]) == 0
    (dave,) = json.loads(capsys.readouterr().out)["vms"]
    assert (dave["bridge"], dave["last_error"]) == ("stopping", None)
    assert calls == ["us-west-2"] * 2 and ssh_hosts(runner) == ["203.0.113.8"]


# ------------------------------------------------ --json and --tag (docs/ADMIN.md §4.1, #312)

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "workshop"
GOLDEN_ROSTER = ROSTER + "dave,Dave,Kim,dave@example.org,d/x,4,ws-dave,203.0.113.8,104,\n"
IDLE_ON = (
    "enabled=enabled\nactive=active\nminutes=--minutes 30\n"
    "last=2026-10-14T16:58:00Z stay up: zab active 3 min ago\n"
)
IDLE_OFF = "enabled=\nactive=inactive\nminutes=\nlast=\n"
STRAINED = (
    "9.70 8.10 6.00 12/400 9999\n8\n"
    "MemTotal:       32212254 kB\nMemAvailable:    2000000 kB\n"
    "/dev/root      101430960 95000000 6430960      94% /\n"
)


def golden_stdout(sections: dict[str, str]) -> str:
    """A VM's answer to STATUS_CMD, in its order, every section present (empty unless given)."""
    names = ("PREFLIGHT", "DOCTOR", "STATUS", "VERSION", "SERVICE", "IDLE", "LOAD")
    body = "".join(f"{st.MARK}{name}\n{sections.get(name, '')}\n\n" for name in names)
    return body + f"{st.MARK}END\n"


GOLDEN_STATUS = {
    "sessions": [
        {"id": "s1", "bot": "codex", "backend": "codex", "state": "idle"},
        {"id": "s2", "bot": "claude", "backend": "claude", "state": "running"},
        {"id": "s3", "bot": "codex", "backend": "codex", "state": "detached"},
    ],
    "running_turns": [{"session_id": "s2", "started_at": 1791996600.0}],  # 2026-10-14 16:50
    "last_activity": 1791996900.5,  # 16:55: idle stop's 16:58 verdict says "3 min ago"
}
DAVE_DOCTOR = [*FLEET_DOCTOR, {"name": "zulip:claude", "ok": False, "detail": "401"}]
GOLDEN_VMS = {
    # alice: zab newer than 0.2.3 (last_activity), a Claude turn running, idle stop on
    "203.0.113.5": golden_stdout(
        {
            "PREFLIGHT": json.dumps(PREFLIGHT),
            "DOCTOR": json.dumps(DOCTOR),
            "STATUS": json.dumps(GOLDEN_STATUS),
            "VERSION": "0.2.4\n",
            "SERVICE": "active",
            "IDLE": IDLE_ON,
            "LOAD": LOAD,
        }
    ),
    # bob: no zab yet, under strain, no idle stop
    "203.0.113.6": golden_stdout({"IDLE": IDLE_OFF, "LOAD": STRAINED}),
    # carol (203.0.113.7) is unreachable, and stopped in EC2 (FakeEc2)
    # dave: zab 0.2.3, whose `zab status --json` has no last_activity; a doctor failure
    "203.0.113.8": golden_stdout(
        {
            "DOCTOR": json.dumps(DAVE_DOCTOR),
            "STATUS": json.dumps({"sessions": [], "running_turns": []}),
            "VERSION": "0.2.3",
            "SERVICE": "active",
            "IDLE": IDLE_OFF,
            "LOAD": LOAD,
        }
    ),
}


class GoldenRunner(FakeRunner):
    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.calls.append(list(argv))
        host = argv[-3].split("@", 1)[1]
        if host in GOLDEN_VMS:
            return ac.RunResult(0, GOLDEN_VMS[host], "")
        return ac.RunResult(255, "", f"ssh: connect to host {host} port 22: No route to host")


def golden_run(tmp_path, monkeypatch, capsys, *extra: str) -> str:
    monkeypatch.setattr(st, "subprocess_runner", GoldenRunner())
    roster = tmp_path / "roster.csv"
    roster.write_text(GOLDEN_ROSTER)
    argv = ["--roster", str(roster), "--known-hosts", str(tmp_path / "kh"), "--parallel", "1"]
    assert st.main([*argv, *extra]) == 0
    return capsys.readouterr().out


def test_the_table_output_is_unchanged(tmp_path, monkeypatch, capsys, fake_ec2):
    """The golden table was printed by status.py as it was before --json (origin/main 8243207),
    for the same VM answers: the new sections and --json change nothing in it. Since #552, a
    disk under strain also names its free space (bob's `94% ! (6.2 GB free)`), which widens
    the disk column; since #588, the `sentinel` column follows `bridge`. Nothing else
    changed."""
    out = golden_run(tmp_path, monkeypatch, capsys)
    assert out == (GOLDEN / "status_table.txt").read_text()


def test_json_golden(tmp_path, monkeypatch, capsys, fake_ec2):
    out = golden_run(tmp_path, monkeypatch, capsys, "--json")
    assert json.loads(out) == json.loads((GOLDEN / "status.json").read_text())


def test_json_with_a_failed_ec2_lookup_and_the_tag(tmp_path, monkeypatch, capsys, fake_ec2):
    fake_ec2.fail = True
    out = json.loads(golden_run(tmp_path, monkeypatch, capsys, "--json"))
    assert out["ec2_error"] == "ImportError: No module named 'boto3'"
    assert out["vms"][2]["bridge"] == "unreachable"
    # --tag scopes the EC2 lookup (FakeEc2 itself asserts the default workshop tag)
    seen = []
    fake_ec2.fail = False
    monkeypatch.setattr(fake_ec2, "find_instances", lambda _, tags, s: seen.append(tags) or [])
    golden_run(tmp_path, monkeypatch, capsys, "--tag", "zab-role=test", "--tag", "workshop=w")
    # both lookups: the first, and again for carol, unreachable since EC2 lists nothing (#439)
    assert seen == [{"zab-role": "test", "workshop": "w"}] * 2
    with pytest.raises(SystemExit):
        st.main(["--roster", "r.csv", "--tag", "participant=x"])  # a per-participant key


def test_the_new_sections_are_fixed_literals_read_without_root():
    cmd = st.STATUS_CMD
    assert "run 'zab --version'" in cmd
    idle = cmd.split(f"echo {st.MARK}IDLE;", 1)[1].split(f"echo {st.MARK}LOAD")[0]
    assert "sudo" not in idle and "run " not in idle
    # the files push_config.py --idle-stop installs, and the log idle_stop.py writes
    assert f"{push_config._UNIT_DIR}/{push_config.IDLE_STOP_UNIT}.service" in idle
    assert f"{push_config.IDLE_STOP_UNIT}.timer" in idle and idle_stop.LOG in idle
    # every substitution is inside double quotes: a verdict is never split or globbed
    subs = [part.strip() for part in idle.split(";") if "$(" in part]
    assert len(subs) == 4 and all(p.startswith('echo "') and p.endswith(')"') for p in subs)


def test_idle_stop_state_and_version_parsing():
    assert st.idle_stop_state(IDLE_ON) == {
        "enabled": True,
        "active": True,
        "minutes": 30,
        "last_verdict": "2026-10-14T16:58:00Z stay up: zab active 3 min ago",
    }
    off = {"enabled": False, "active": False, "minutes": None, "last_verdict": None}
    assert st.idle_stop_state(IDLE_OFF) == off
    assert st.idle_stop_state("enabled=not-found\nactive=inactive\n") == off
    assert st.idle_stop_state("") is None
    assert st.zab_version("0.2.4\n") == "0.2.4" and st.zab_version("0.3.0rc1") == "0.3.0rc1"
    assert st.zab_version("bash: zab: command not found") is None and st.zab_version("") is None
    assert st.iso_time(0) == "1970-01-01T00:00:00Z" and st.iso_time(None) is None
    assert st.iso_time(True) is None and st.iso_time("1760461080") is None


#: A VM's owner controls what it answers (reviewer on #320): each case is one VM's stdout.
HOSTILE_STATUS = (
    '{"sessions": [{"id": ["x"], "bot": {"a": 1}, "backend": NaN, "state": "running"}], '
    '"running_turns": [{"session_id": ["x"], "started_at": 1e300}], "last_activity": "soon"}'
)
HOSTILE = {
    "turns-text": golden_stdout({"STATUS": '{"sessions": [], "running_turns": "xyz"}'}),
    "turns-numbers": golden_stdout({"STATUS": '{"sessions": [], "running_turns": [1, 2]}'}),
    "unhashable-ids": golden_stdout({"STATUS": HOSTILE_STATUS, "SERVICE": "active"}),
    "names-and-times": golden_stdout(
        {
            "STATUS": json.dumps(
                {
                    "sessions": [
                        {"id": "s1", "bot": "b" * 5000, "backend": "codex", "state": "idle"},
                        {"id": "s2", "bot": "ok_bot-2", "backend": "x\ny", "state": "running"},
                    ],
                    "running_turns": [
                        {"session_id": "s1", "started_at": 10**30},
                        {"session_id": "s2", "started_at": -5},
                        {"session_id": 7, "started_at": 1791996600},
                    ],
                    "last_activity": 1e300,
                }
            ),
        }
    ),
    "many-turns": golden_stdout(
        {"STATUS": json.dumps({"sessions": [], "running_turns": [{"session_id": "s"}] * 500})}
    ),
    "idle-version-load": golden_stdout(
        {
            "VERSION": "9" * 5000,
            "IDLE": f"enabled=enabled\nactive=active\nminutes=--minutes {'9' * 5000}\n"
            f"last={'x' * 10000}\n",
            "LOAD": "nan 0.30 0.20 1/234 5678\n8\nMemTotal: 99999999999999999999 kB\n"
            "MemAvailable: 1 kB\n/dev/root 1 2 3 101% /\n",
        }
    ),
    "infinite-load": golden_stdout({"LOAD": "1e400 0.30 0.20 1/234 5678\n8\n"}),
    "huge-disk": golden_stdout({"LOAD": f"/dev/root {2**40 + 1} 1 1 50% /\n"}),  # over 1 PiB
}


def test_hostile_vm_output_degrades_to_null_and_the_table_is_mains(fake_ec2):
    """Every case parses (no exception, so no error row), its table row is the one
    origin/main's status.py prints for it (status_hostile_rows.json; since #327 `turns-text`'s
    text counts as no running turn, not as its 3 characters), and its JSON is strict: names that
    zab would not write, implausible numbers and times are null, lists are cut."""
    mains = json.loads((GOLDEN / "status_hostile_rows.json").read_text())
    vms = {name: st.summarize({"slug": name, "host": "h"}, out) for name, out in HOSTILE.items()}
    assert {name: vm.row() for name, vm in vms.items()} == mains
    doc = json.loads(json.dumps(st.fleet_json(list(vms.values())), allow_nan=False))
    by = {vm["slug"]: vm for vm in doc["vms"]}
    assert (
        by["turns-text"]["running_turns"] is None and by["turns-numbers"]["running_turns"] is None
    )
    nobody = {"bot": None, "backend": None, "started_at": None}
    assert by["unhashable-ids"]["running_turns"] == [nobody]
    assert by["unhashable-ids"]["last_activity"] is None
    assert by["names-and-times"]["running_turns"] == [
        {"bot": None, "backend": "codex", "started_at": None},
        {"bot": "ok_bot-2", "backend": None, "started_at": None},
        {**nobody, "started_at": "2026-10-14T16:50:00Z"},
    ]
    assert by["names-and-times"]["last_activity"] is None
    assert len(by["many-turns"]["running_turns"]) == st.MAX_ITEMS
    vm = by["idle-version-load"]
    assert vm["zab_version"] is None and vm["idle_stop"]["minutes"] is None
    assert len(vm["idle_stop"]["last_verdict"]) == 300
    assert (vm["load"], vm["memory"], vm["disk_percent"], vm["disk"]) == (None,) * 4
    assert by["infinite-load"]["load"] is None
    assert by["huge-disk"]["disk"] is None and by["huge-disk"]["disk_percent"] == 50


@pytest.mark.parametrize(
    ("status", "sessions"),
    [
        ('{"sessions": [], "running_turns": 5}', "0 (0 running)"),
        ('{"sessions": {}, "running_turns": []}', "0 (0 running)"),
        ('{"sessions": {"s1": {"state": "idle"}}, "running_turns": [{}]}', "0 (1 running)"),
        ('{"sessions": ["idle", 5, null, {"state": "running"}]}', "1 (0 running)"),
        ('{"sessions": [], "running_turns": [], "n": 1' + "0" * 5000 + "}", "?"),
    ],
)
def test_a_status_of_the_wrong_shape_reads_as_nothing_not_an_error_row(status, sessions):
    """A VM's owner controls its STATUS section (#327): a `running_turns` or `sessions` that
    isn't a list counts as none, a session that isn't an object is skipped, and JSON that Python
    refuses (an integer of over 4300 digits raises a plain `ValueError`) reads as no answer."""
    vm = st.summarize({"slug": "x", "host": "h"}, golden_stdout({"STATUS": status}))
    assert vm.sessions == sessions and vm.last_error == ""
    assert vm.bridge == ("no zab" if sessions == "?" else "?")


@pytest.mark.parametrize(
    "sections",
    [
        {"DOCTOR": "[5, null]"},
        {"PREFLIGHT": '{"checks": [5, "x"]}'},
        {"STATUS": "[" * 100_000 + "]" * 100_000},  # deeper than the parser's stack
        {"DOCTOR": "[" * 100_000},
    ],
)
def test_checks_of_the_wrong_shape_and_deep_json_read_as_nothing(sections):
    """The review of #576: a doctor or preflight check that isn't an object is skipped, and
    JSON nested deeper than Python parses (`RecursionError`) reads as no answer."""
    vm = st.summarize({"slug": "x", "host": "h"}, golden_stdout(sections))
    assert vm.last_error == "" and vm.doctor_failures == []
    assert (vm.codex_login, vm.claude_login, vm.git_push) == ("?", "?", "?")


def test_zab_status_last_activity_counts_what_idle_stop_counts(tmp_path):
    """V7 (docs/ADMIN.md §9): `zab status --json`'s `last_activity` is idle_stop.py's newest
    activity, read from the same database: messages the bridge ignored never count."""
    config = SimpleNamespace(bridge=SimpleNamespace(state_dir=str(tmp_path)))
    store = Store(tmp_path)
    db = str(tmp_path / "zab.db")

    def both():
        return read_status(config)["last_activity"], idle_stop.read_db(db)["last"]

    session = dict(
        id="s", bot="b", backend="codex", native_id="n", card_message_id=1, stream_id=1,
        topic="t", workspace="/w", workspace_mode="shared", repo_root="/w", settings_json="{}",
        state="idle", created_at=0, updated_at=0,
    )  # fmt: skip
    try:
        assert both() == (None, None)
        store.insert("sessions", **session)
        store.insert(
            "turns", session_id="s", native_turn_id="t", trigger_msg_ids="[]", state="done",
            started_at=100, ended_at=200,
        )  # fmt: skip
        assert both() == (200, 200)
        store.insert("inbound", message_id=1, bot="b", disposition="ignored", handled_at=900)
        assert both() == (200, 200)  # someone else's post in a shared channel
        store.insert("held", message_id=2, session_id="s", sender="x", content="c", received_at=300)
        assert both() == (300, 300)
        store.insert("inbound", message_id=3, bot="b", disposition="turn", handled_at=400)
        assert both() == (400, 400)
    finally:
        store.close()
