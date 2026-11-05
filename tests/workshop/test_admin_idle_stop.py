"""`workshop/admin/idle_stop.py` (issue #207): a kept VM powers off only when nobody uses it."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace

import aws_common
import idle_stop as idle
import pytest

from zab.core.store import Store

NOW = 1_800_000_000.0
ME = os.getuid()  # stands for ubuntu where its keys are read


class FakeRun:
    """`subprocess.run` for the fixed commands: `who`, systemctl and the agent child."""

    def __init__(
        self,
        *,
        who: str = "",
        zab: str = "active",
        codex: str | None = "",
        poweroff: int = 0,
        warm: str = "inactive\nactive\n",
    ):
        self.calls: list[tuple[list[str], dict]] = []
        self.who = who
        self.zab = zab
        self.codex = codex  # active units' list-units lines; None: systemctl fails
        self.poweroff = poweroff
        self.warm = warm  # `systemctl is-active` of the warm-up's unit and cloud-final (#715)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv == ["who"]:
            return subprocess.CompletedProcess(argv, 0, self.who, "")
        if argv == idle.WARM_ACTIVE:
            return subprocess.CompletedProcess(argv, 0, self.warm, "")
        if argv == idle.ZAB_ACTIVE:
            return subprocess.CompletedProcess(argv, 0 if self.zab == "active" else 3, self.zab, "")
        if argv == idle.CODEX_ACTIVE:
            if self.codex is None:
                return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
            return subprocess.CompletedProcess(argv, 0, self.codex, "")
        if argv == idle.POWEROFF:
            return subprocess.CompletedProcess(argv, self.poweroff, "", "")
        raise AssertionError(f"unexpected command {argv}")

    @property
    def powered_off(self) -> bool:
        return any(argv == idle.POWEROFF for argv, _ in self.calls)


@pytest.fixture
def proc(tmp_path):
    """A fake /proc: up 2 hours, load 0.05 over 5 minutes, no processes."""
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("7200.00 50000.00\n")
    (root / "loadavg").write_text("0.10 0.05 0.01 1/300 4242\n")
    return root


@pytest.fixture
def db(tmp_path):
    """zab's real schema, with no rows yet."""
    store = Store(tmp_path / "state")
    store.db.close()
    return tmp_path / "state" / "zab.db"


def add_rows(
    db,
    *,
    handled=None,
    disposition="turn",
    turn_started=None,
    turn_ended=None,
    running=False,
    held=None,
    backend="claude",
    pgid=None,
):
    con = sqlite3.connect(db)
    if handled is not None:
        con.execute(
            "INSERT INTO inbound(message_id, bot, disposition, handled_at) VALUES (1, 'b', ?, ?)",
            (disposition, handled),
        )
    if turn_started is not None:
        con.execute(
            "INSERT INTO sessions (id, bot, backend, native_id, card_message_id, stream_id,"
            " topic, workspace, workspace_mode, repo_root, settings_json, state, created_at,"
            " updated_at) VALUES ('s',?,?,'n',1,1,'t','w','worktree','/r','{}','idle',0,0)",
            ("b", backend),
        )
        con.execute(
            "INSERT INTO turns(session_id, trigger_msg_ids, state, started_at, ended_at, pgid) "
            "VALUES ('s', '[]', ?, ?, ?, ?)",
            ("running" if running else "completed", turn_started, turn_ended, pgid),
        )
    if held is not None:
        con.execute("INSERT INTO held VALUES (2, 's', 'Alice', 'hi', ?)", (held,))
    con.commit()
    con.close()


def facts(**changes) -> idle.Facts:
    base = idle.Facts(now=NOW, uptime=7200.0, load5=0.05, last_activity=NOW - 3600)
    for name, value in changes.items():
        setattr(base, name, value)
    return base


def test_an_idle_vm_has_no_reason_to_stay_up():
    assert idle.hold_reasons(facts(), 15, 0.5) == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"uptime": 600.0}, "up only 10 min"),
        ({"running_turns": 1}, "1 turn(s) running"),
        ({"last_activity": NOW - 120}, "zab active 2 min ago"),
        ({"load5": 0.5}, "load 0.50"),
        ({"logins": ["ubuntu@pts/0"]}, "logged in: ubuntu@pts/0"),
        ({"uptime": None}, "uptime unknown"),
        ({"load5": None}, "load unknown"),
        ({"running_turns": None}, "running turns unknown"),
        ({"errors": ["logins (who exited 1)"]}, "cannot read logins (who exited 1)"),
    ],
)
def test_each_check_alone_keeps_the_vm_up(changes, reason):
    assert idle.hold_reasons(facts(**changes), 15, 0.5) == [reason]


def row(backend="claude", started=NOW - 600, pgid=None, state="idle", approval=False) -> dict:
    return {
        "backend": backend,
        "session_state": state,
        "started_at": started,
        "pgid": pgid,
        "approval": approval,
    }


@pytest.mark.parametrize(
    ("rows", "zab", "codex", "counted", "ignored"),
    [
        ([row()], "active", "", 1, []),
        # Claude turns die with the bridge: rows left over from a crash don't hold it up
        ([row()], "failed", "", 0, ["a claude row with the bridge down"]),
        # Codex turns run in the daemon (PM on #218): they count while it is up
        ([row("codex")], "failed", "codex-app-server.service loaded active running x\n", 1, []),
        ([row("codex")], "inactive", "", 0, ["a codex row with the bridge and the daemon down"]),
        ([row("codex")], "failed", None, 1, []),  # the daemon's state unknown: it counts
        ([row()], "unknown", "", 1, []),  # zab's state unknown: it counts
        # age never decides (PM on #236): detached, old, no approval, and still counted
        ([row(state="detached")], "active", "", 1, []),
        ([row("codex", started=NOW - 90_000)], "active", "", 1, []),
        ([row("codex", started=NOW - 90_000, state="detached")], "active", "", 1, []),
        ([row(started=NOW - 90_000, state="detached")], "active", "", 1, []),
    ],
)
def test_which_running_rows_hold_the_vm_up(rows, zab, codex, counted, ignored):
    run = FakeRun(zab=zab, codex=codex)
    got, notes, _suspects = idle.count_running(rows, NOW, run)
    assert (got, notes) == (counted, ignored)


def test_an_old_detached_row_without_a_process_is_named_as_possibly_stale():
    """PM on #236: it keeps the VM up, and the verdict says that it might be stale."""
    old = row("codex", started=NOW - 31 * 3600, state="detached")
    counted, _, suspects = idle.count_running([old], NOW, FakeRun())
    assert (counted, suspects) == (1, ["a detached codex turn running 31 h"])
    f = facts(running_turns=1, suspect_rows=suspects)
    assert idle.hold_reasons(f, 15, 0.5) == [
        "staying up only for a detached codex turn running 31 h "
        "(possibly stale: check with `zab status`)"
    ]
    # with another running turn, the usual reason (the suspect isn't the only thing)
    f = facts(running_turns=2, suspect_rows=suspects)
    assert idle.hold_reasons(f, 15, 0.5) == ["2 turn(s) running"]
    # a pending approval, or a recorded process, makes it no suspect at all
    for live in (
        row("codex", started=NOW - 31 * 3600, state="detached", approval=True),
        row(started=NOW - 31 * 3600, state="detached", pgid=42),
    ):
        assert idle.count_running([live], NOW, FakeRun(), alive=lambda pgid: True)[2] == []


def test_a_live_process_counts_at_any_age_even_detached():
    """Codex on #236: the age cutoff skipped the process check and stopped a live turn, and
    `!detach` keeps a running turn."""
    old_live = row(started=NOW - 90_000, pgid=4242, state="detached")
    counted, ignored, suspects = idle.count_running(
        [old_live], NOW, FakeRun(), alive=lambda pgid: True
    )
    assert (counted, ignored, suspects) == (1, [], [])


def test_a_claude_row_whose_process_group_is_gone_does_not_count():
    rows = [row(pgid=4242), row(pgid=5151)]
    alive = {4242: False, 5151: True}
    counted, ignored, _ = idle.count_running(rows, NOW, FakeRun(), alive=alive.__getitem__)
    assert (counted, ignored) == (1, ["a claude row whose process is gone"])


def test_ignored_rows_are_named_in_the_verdict():
    f = facts(running_turns=0, ignored_rows=["a claude row whose process is gone"])
    assert idle.verdict(f, [], 15).endswith("(ignoring a claude row whose process is gone)")


def test_a_vm_that_was_never_used_stops_after_the_idle_time():
    assert idle.hold_reasons(facts(last_activity=None), 15, 0.5) == []
    assert idle.hold_reasons(facts(last_activity=None, uptime=60.0), 15, 0.5) == ["up only 1 min"]


def test_read_db_counts_running_turns_and_finds_the_newest_activity(db):
    assert idle.read_db(str(db)) == {"running": [], "last": None}
    add_rows(db, handled=NOW - 900, turn_started=NOW - 800, turn_ended=NOW - 100)
    assert idle.read_db(str(db)) == {"running": [], "last": NOW - 100}


def test_messages_the_bridge_ignored_are_not_activity(db):
    """Codex on #218: `Store.claim` records every message a bot sees as `ignored` first, other
    people's posts in a shared channel included; only what reached an agent keeps the VM up."""
    add_rows(db, handled=NOW - 60, disposition="ignored")
    assert idle.read_db(str(db)) == {"running": [], "last": None}


@pytest.mark.parametrize("disposition", ["turn", "delivered", "attach"])
def test_messages_for_an_agent_are_activity(db, disposition):
    add_rows(db, handled=NOW - 60, disposition=disposition)
    assert idle.read_db(str(db)) == {"running": [], "last": NOW - 60}


def test_a_message_held_for_a_busy_session_is_activity(db):
    add_rows(db, turn_started=NOW - 7000, turn_ended=NOW - 6000, held=NOW - 30)
    assert idle.read_db(str(db)) == {"running": [], "last": NOW - 30}


def test_read_db_sees_a_running_turn(db):
    add_rows(db, handled=NOW - 900, turn_started=NOW - 800, running=True, backend="codex", pgid=7)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO approvals VALUES ('r1', 's', NULL, 'command', 'pending', ?)", (NOW,))
    con.commit()
    con.close()
    assert idle.read_db(str(db)) == {
        "running": [
            {
                "backend": "codex",
                "session_state": "idle",
                "started_at": NOW - 800,
                "pgid": 7,
                "approval": True,
            }
        ],
        "last": NOW - 800,
    }


def test_a_missing_database_keeps_the_vm_up(tmp_path, proc):
    """PM on #218: "anything it cannot read keeps the VM up" includes a missing database."""
    assert idle.read_db(str(tmp_path / "none.db")) == {"missing": True}
    got = gather(tmp_path, proc, tmp_path / "none.db", FakeRun())
    assert "cannot read zab's database: missing" in idle.hold_reasons(got, 15, 0.5)


def test_an_unreadable_database_keeps_the_vm_up(tmp_path, proc):
    bad = tmp_path / "zab.db"
    bad.write_text("not a database")
    got = gather(tmp_path, proc, bad, FakeRun())
    assert got.running_turns is None
    assert any("zab's database" in r for r in idle.hold_reasons(got, 15, 0.5))


def test_a_child_that_times_out_keeps_the_vm_up(tmp_path, proc, db):
    """P3 from the PM's review: `TimeoutExpired` escaped and failed the unit."""

    def run(argv, **kwargs):
        if argv[-2:-1] == ["--read-db"]:
            raise subprocess.TimeoutExpired(argv, 60)
        return FakeRun()(argv, **kwargs)

    got = idle.gather(str(db), run, proc=str(proc), now=NOW, euid=0, login="/x", hold="/x")
    assert got.running_turns is None and "zab's database" in got.errors[0]


def gather(tmp_path, proc, db, run, **kw):
    """`idle.gather` as a non-root user, with the login and hold files under tmp_path."""
    kw.setdefault("login", str(tmp_path / "login"))
    kw.setdefault("hold", str(tmp_path / "hold"))
    return idle.gather(str(db), run, proc=str(proc), now=NOW, euid=1000, **kw)


def test_as_root_the_database_is_read_by_agent():
    run = FakeRun()

    def child(argv, **kwargs):
        run.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"running": [], "last": 5.0}\n', "")

    assert idle.read_db_as_agent("/x/zab.db", child) == {"running": [], "last": 5.0}
    argv, kwargs = run.calls[0]
    assert argv[-2:] == ["--read-db", "/x/zab.db"]
    assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == ("agent", "agent", [])


def test_ssh_sessions_come_from_sshd_process_titles(proc):
    titles = {
        "101": b"sshd: ubuntu@notty\0",
        "102": b"sshd-session: drew@pts/0\0",
        "103": b"sshd: ubuntu [priv]\0",
        "104": b"sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups\0",
        "105": b"/usr/bin/python3\0-c\0sshd: fake@x\0",
    }
    for pid, title in titles.items():
        (proc / pid).mkdir()
        (proc / pid / "cmdline").write_bytes(title)
    assert sorted(idle.ssh_sessions(str(proc))) == ["drew@pts/0", "ubuntu@notty"]


def test_who_lines_are_logins():
    run = FakeRun(who="ubuntu   pts/0        2026-10-11 12:00 (198.51.100.1)\n")
    assert idle.who_logins(run) == ["ubuntu@pts/0"]


def run_main(tmp_path, proc, db, run, *extra, minutes="15", **kwargs):
    kwargs.setdefault("read_home", str(tmp_path / "no-read-user"))
    return idle.main(
        ["--minutes", minutes, "--db", str(db), *extra],
        run=run,
        proc=str(proc),
        now=NOW,
        log=str(tmp_path / "idle.log"),
        last=str(tmp_path / "last"),
        login=str(tmp_path / "login"),
        hold=str(tmp_path / "hold"),
        euid=1000,  # not root, whoever runs the tests (PM on #218: 8 of 21 failed as root)
        **kwargs,
    )


def test_main_powers_off_an_idle_vm_and_logs_why(tmp_path, proc, db, capsys):
    add_rows(db, handled=NOW - 3600)
    con = sqlite3.connect(db)  # someone else chatting in a shared channel a minute ago
    con.execute("INSERT INTO inbound VALUES (9, 'b', NULL, 'ignored', ?)", (NOW - 60,))
    con.commit()
    con.close()
    run = FakeRun()
    assert run_main(tmp_path, proc, db, run) == 0
    assert run.powered_off
    line = (tmp_path / "idle.log").read_text()
    assert "power off: idle for 15 min" in line and "zab idle 60 min" in line
    assert "power off" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ({"handled": NOW - 60}, "zab active 1 min ago"),
        ({"turn_started": NOW - 7000, "running": True}, "1 turn(s) running"),
    ],
)
def test_main_keeps_a_busy_vm_up(tmp_path, proc, db, setup, why):
    add_rows(db, **setup)
    run = FakeRun()
    assert run_main(tmp_path, proc, db, run) == 0
    assert not run.powered_off
    assert why in (tmp_path / "idle.log").read_text()


def test_main_keeps_a_loaded_or_logged_in_vm_up(tmp_path, proc, db):
    (proc / "loadavg").write_text("3.00 2.10 1.00 4/300 4242\n")
    run = FakeRun(who="ubuntu pts/0 2026-10-11 12:00\n")
    run_main(tmp_path, proc, db, run)
    assert not run.powered_off
    log = (tmp_path / "idle.log").read_text()
    assert "load 2.10" in log and "logged in: ubuntu@pts/0" in log


def test_the_log_gets_a_line_only_when_the_verdict_changes(tmp_path, proc, db):
    add_rows(db, handled=NOW - 60)
    for _ in range(3):
        run_main(tmp_path, proc, db, FakeRun())
    assert len((tmp_path / "idle.log").read_text().splitlines()) == 1
    (proc / "loadavg").write_text("3.00 2.10 1.00 4/300 4242\n")
    run_main(tmp_path, proc, db, FakeRun())
    assert len((tmp_path / "idle.log").read_text().splitlines()) == 2


def test_dry_run_never_powers_off(tmp_path, proc, db):
    run = FakeRun()
    run_main(tmp_path, proc, db, run, "--dry-run")
    assert not run.powered_off
    assert "dry run: not powering off" in (tmp_path / "idle.log").read_text()


def test_a_failed_poweroff_is_an_error(tmp_path, proc, db):
    assert run_main(tmp_path, proc, db, FakeRun(poweroff=1)) == 1


def test_minutes_are_required_and_at_least_ten(tmp_path, proc, db):
    with pytest.raises(SystemExit):
        idle.main(["--db", str(db)], run=FakeRun(), proc=str(proc))
    with pytest.raises(SystemExit):
        run_main(tmp_path, proc, db, FakeRun(), minutes="5")


def test_a_logout_keeps_the_vm_up_for_the_idle_time(tmp_path, proc, db):
    """PM on #218: closing the last SSH session must not stop the VM within a minute."""
    add_rows(db, handled=NOW - 3600)
    run_main(tmp_path, proc, db, FakeRun(who="drew pts/0 2026-10-11 12:00\n"))
    login = tmp_path / "login"
    assert login.exists()  # the last time a login was seen
    os.utime(login, (NOW - 180, NOW - 180))
    run = FakeRun()  # logged out three minutes ago
    run_main(tmp_path, proc, db, run)
    assert not run.powered_off
    assert "logged in 3 min ago" in (tmp_path / "idle.log").read_text()
    os.utime(login, (NOW - 1000, NOW - 1000))  # more than 15 minutes ago
    run_main(tmp_path, proc, db, run)
    assert run.powered_off


def test_a_rollout_hold_keeps_the_vm_up_until_it_expires(tmp_path, proc, db):
    add_rows(db, handled=NOW - 3600)
    hold = tmp_path / "hold"
    hold.write_text(f"{int(NOW + 600)}\n")
    run = FakeRun()
    run_main(tmp_path, proc, db, run)
    assert not run.powered_off and "held by a rollout until" in (tmp_path / "idle.log").read_text()
    hold.write_text(f"{int(NOW - 1)}\n")  # expired: a rollout that died without lifting it
    run_main(tmp_path, proc, db, run)
    assert run.powered_off


def test_an_unreadable_hold_counts_for_an_hour_from_its_mtime(tmp_path):
    hold = tmp_path / "hold"
    hold.write_text("garbage")
    os.utime(hold, (NOW - 600, NOW - 600))
    assert idle.read_hold(str(hold)) == NOW - 600 + 3600
    assert idle.read_hold(str(tmp_path / "none")) is None


# ------------------------------------------------------------------- the read user (#507)

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIhyFisRM1M4HCLLLSCljthiH4FEA8rHJ3GHqLbs1u7R"
EC = (
    "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBL2Zdq0n2INYTvom7gLh"
    "lppINTazry8pmO96lvaD+xzFG2zXH5JjliIUPt+QnxY3h0rHgGMsJdBgf+fnQJHrteM="
)


def sessions(proc, start, *titles):
    for pid, title in enumerate(titles, start):
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "cmdline").write_bytes(title.encode() + b"\0")


def test_status_reads_are_not_logins(tmp_path, proc, db, capsys):
    """zab-monitor's sessions without a terminal, the admin server's reads, neither keep the VM
    up nor restart the grace; anyone else's do, and so would a terminal of that user."""
    add_rows(db, handled=NOW - 3600)
    reads = (
        "sshd: zab-monitor@notty",
        "sshd-session: zab-monitor@notty",
        "sshd: zab-monitor [priv]",
    )
    sessions(proc, 200, *reads)
    run = FakeRun()
    run_main(tmp_path, proc, db, run)
    assert run.powered_off and not (tmp_path / "login").exists()
    assert "not logins: zab-monitor@notty, zab-monitor@notty" in capsys.readouterr().out
    sessions(proc, 300, "sshd: ubuntu@notty", "sshd-session: zab-monitor@pts/1")
    run = FakeRun()
    run_main(tmp_path, proc, db, run)
    assert not run.powered_off and (tmp_path / "login").exists()
    assert "logged in: ubuntu@notty, zab-monitor@pts/1" in (tmp_path / "idle.log").read_text()


def test_read_keys_restrict_each_plain_key_of_ubuntus(tmp_path):
    text = (
        f"{ED} zab-admin\n  {EC}\r\n{ED} the same key again\n"
        f'from="10.0.0.1" {EC} options: never loosened\ncert-authority {ED}\n'
        "# a comment\n\nssh-dss AAAAB3NzaC1kc3M old\nssh-ed25519\n"
    )
    want = f"{idle.READ_OPTIONS} {ED}\n{idle.READ_OPTIONS} {EC}\n"
    assert idle.read_keys(text) == want
    assert idle.READ_OPTIONS == (
        'restrict,command="/usr/bin/sudo -n -H -u agent /usr/local/lib/zab/status_read.sh"'
    )
    assert idle.KEY_TYPES == aws_common.PUBKEY_TYPES
    keygen = shutil.which("ssh-keygen")
    if keygen:  # OpenSSH's own parser reads each line as a key with its options
        (tmp_path / "keys").write_text(want)
        res = subprocess.run([keygen, "-lf", str(tmp_path / "keys")], capture_output=True)  # noqa: S603
        assert res.returncode == 0 and len(res.stdout.splitlines()) == 2


def test_sync_read_keys_follows_ubuntus_file(tmp_path):
    src, home = tmp_path / "ubuntu-keys", tmp_path / "home"
    dst = home / ".ssh" / "authorized_keys"
    src.write_text(f"{ED} a\n{EC} b\n")

    def sync():
        return idle.sync_read_keys(str(src), str(dst), ME)  # ME stands for ubuntu

    assert sync() is None  # not set up: nothing written
    (home / ".ssh").mkdir(parents=True)
    assert sync() is True
    assert dst.read_text() == idle.read_keys(src.read_text())
    assert dst.stat().st_mode & 0o777 == 0o644
    assert sync() is False  # unchanged: not rewritten
    src.write_text(f"{EC} b\n")  # a key removed for ubuntu stops reading at the next tick
    assert sync() is True and ED not in dst.read_text()
    src.unlink()
    assert sync() is True and dst.read_text() == ""


def test_each_tick_copies_the_keys_and_a_failure_changes_no_verdict(
    tmp_path, proc, db, capsys, monkeypatch
):
    add_rows(db, handled=NOW - 60)
    home, src = tmp_path / "home", tmp_path / "ubuntu-keys"
    (home / ".ssh").mkdir(parents=True)
    src.write_text(f"{ED}\n")
    paths = {"read_home": str(home), "ubuntu_keys": str(src), "keys_owner": ME}
    run_main(tmp_path, proc, db, FakeRun(), "--dry-run", **paths)
    assert not (home / ".ssh" / "authorized_keys").exists()  # a dry run changes nothing
    run_main(tmp_path, proc, db, FakeRun(), **paths)
    assert ED in (home / ".ssh" / "authorized_keys").read_text()
    src.unlink()
    src.mkdir()  # unreadable
    capsys.readouterr()
    run_main(tmp_path, proc, db, FakeRun(), **paths)
    out = capsys.readouterr()
    assert "cannot copy ubuntu's keys for zab-monitor" in out.err
    assert "stay up: zab active 1 min ago" in out.out
    src.rmdir()
    src.write_text(f"{ED}\n")

    def boom(*args):
        raise MemoryError

    monkeypatch.setattr(idle, "sync_read_keys", boom)  # anything at all: still a verdict
    run_main(tmp_path, proc, db, FakeRun(), **paths)
    out = capsys.readouterr()
    assert "MemoryError" in out.err and "stay up: zab active 1 min ago" in out.out


def test_setup_read_user_creates_or_resets_it_with_roots_home(tmp_path, monkeypatch):
    calls, owned = [], []

    def missing(name):
        raise KeyError(name)

    def run(argv, **kwargs):
        calls.append(argv)

    monkeypatch.setattr(idle.pwd, "getpwnam", missing)
    src, home = tmp_path / "ubuntu-keys", tmp_path / "home"
    src.write_text(f"{ED}\n")
    paths = {"home": str(home), "src": str(src), "owner": ME}
    idle.setup_read_user(run, **paths, chown=lambda *a: owned.append(a))
    argv = ["useradd", "--system", "--no-create-home", "--home-dir", str(home)]
    assert calls == [[*argv, "--shell", "/usr/local/lib/zab/status_read_login", "zab-monitor"]]
    assert owned == [(str(home), 0, 0), (f"{home}/.ssh", 0, 0)]
    assert (home / ".ssh").stat().st_mode & 0o777 == 0o755
    assert ED in (home / ".ssh" / "authorized_keys").read_text()
    # one that exists (an earlier install, or a VM someone changed) is reset, not trusted; its
    # home apart, since usermod refuses that alone while a read runs (the review of #514)
    reset = ["usermod", "--shell", idle.READ_LOGIN, "--groups", "", "--lock", "zab-monitor"]
    rehome = ["usermod", "--home", str(home), "zab-monitor"]
    for pw_dir, want in ((str(home), [reset]), ("/home/elsewhere", [reset, rehome])):
        monkeypatch.setattr(idle.pwd, "getpwnam", lambda name, d=pw_dir: SimpleNamespace(pw_dir=d))
        calls.clear()
        idle.setup_read_user(run, **paths, chown=lambda *a: None)
        assert calls == want


def test_the_login_shell_is_the_read_whatever_it_is_asked():
    """Every way in as zab-monitor, keys from outside the file included (EC2 Instance Connect),
    runs the read: sshd hands the shell `-c <command>`, which it never looks at."""
    assert idle.READ_LOGIN_TEXT.startswith("#!/bin/sh\n")
    assert idle.READ_LOGIN_TEXT.endswith(f"\nexec {idle.READ_COMMAND}\n")
    assert "$" not in idle.READ_LOGIN_TEXT  # neither its arguments nor anything else


def test_ubuntus_keys_are_read_with_care(tmp_path):
    """As root: neither `.ssh` nor the file through a symlink, both ubuntu's (here: ME), never
    stuck on a FIFO or a device, never more than MAX_KEYS_BYTES; and then no key reads (the copy
    is emptied) until the file is sane again."""
    home, ssh = tmp_path / "home", tmp_path / "ssh"
    dst = home / ".ssh" / "authorized_keys"
    (home / ".ssh").mkdir(parents=True)
    ssh.mkdir()
    (ssh / "authorized_keys").write_text(f"{ED}\n")
    assert idle.ubuntu_keys_text(str(ssh / "authorized_keys"), ME) == f"{ED}\n"
    assert idle.ubuntu_keys_text(str(ssh / "none"), ME) == ""
    assert idle.ubuntu_keys_text(str(tmp_path / "none" / "authorized_keys"), ME) == ""
    (tmp_path / "linked").symlink_to(ssh)  # a symlinked .ssh, pointing anywhere
    (ssh / "link").symlink_to(ssh / "authorized_keys")
    os.mkfifo(ssh / "fifo")
    (ssh / "big").write_text("#" * idle.MAX_KEYS_BYTES + "\n")
    cases = [
        (tmp_path / "linked" / "authorized_keys", ME, "Not a directory"),  # O_NOFOLLOW
        (ssh / "link", ME, "Too many levels"),
        (ssh / "authorized_keys", ME + 1, "is not ubuntu's"),  # a .ssh someone else owns
        (ssh / "fifo", ME, "not a regular"),
        ("/dev/zero", os.stat("/dev").st_uid, "not a regular"),
        (ssh / "big", ME, "larger than"),
    ]
    tmp = os.stat("/tmp").st_uid  # noqa: S108 - a directory someone else owns, if not ME
    with tempfile.NamedTemporaryFile(dir="/tmp") as theirs:
        if tmp != ME:  # a file in it that isn't its owner's: a hard link would be such a file
            cases.append((theirs.name, tmp, "is not ubuntu's"))
        for bad, owner, why in cases:
            dst.write_text("a key from before\n")
            with pytest.raises(OSError, match=why):
                idle.sync_read_keys(str(bad), str(dst), owner)
            assert dst.read_text() == "", bad


# ------------------------------------------------------- the first boot's warm-up (#715)


@pytest.mark.parametrize(
    ("states", "running"),
    [
        ("active\nactive\n", True),  # the warm-up's unit runs
        ("inactive\nactivating\n", True),  # cloud-init's last stage, which starts it, runs
        ("activating\nactive\n", True),
        ("inactive\nactive\n", False),  # cut off by a stop, or an image without it
        ("failed\nactive\n", False),
        ("inactive\nfailed\n", False),
        ("", None),  # systemctl said nothing it can read: unknown, so it holds
        ("active\n", None),
        ("bogus\nactive\n", None),
    ],
)
def test_the_warm_up_holds_while_it_can_still_end(tmp_path, states, running):
    """#715: the marker missing, and the unit or cloud-final still up; never once it's there."""
    marker = tmp_path / "zab-warm.done"
    assert idle.warm_up_running(FakeRun(warm=states), str(marker)) is running
    marker.touch()
    run = FakeRun(warm=states)
    assert idle.warm_up_running(run, str(marker)) is False
    assert not run.calls  # the marker alone answers: systemctl isn't asked


def test_the_warm_up_hold_is_named_in_the_verdict(tmp_path, proc, db):
    """#715: the verdict names it, with the uptime past; unknown holds too."""
    hold = ["the first boot's volume warm-up is running"]
    assert idle.hold_reasons(facts(warming=True), 10, 0.5) == hold
    unknown = ["the first boot's volume warm-up unknown"]
    assert idle.hold_reasons(facts(warming=None), 10, 0.5) == unknown
    run, warm = FakeRun(warm="active\nactive\n"), str(tmp_path / "zab-warm.done")
    assert run_main(tmp_path, proc, db, run, minutes="10", warm=warm) == 0
    assert not run.powered_off
    assert f"stay up: {hold[0]}" in (tmp_path / "idle.log").read_text()
    (tmp_path / "zab-warm.done").touch()  # ended: the same VM powers off at the next tick
    run = FakeRun(warm="active\nactive\n")
    assert run_main(tmp_path, proc, db, run, minutes="10", warm=warm) == 0
    assert run.powered_off


@pytest.mark.parametrize("states", ["inactive\nactivating\n", "bogus\nactive\n"])
def test_the_warm_up_holds_no_longer_than_three_hours_up(tmp_path, proc, db, states):
    """Review of #728: a cloud-final hung in `activating`, or an answer it can't read, holds the
    VM up to `WARM_MAX_S` of uptime, then no more; the verdict says what it ignores."""
    warm = str(tmp_path / "zab-warm.done")
    (proc / "uptime").write_text(f"{idle.WARM_MAX_S - 60}.00 1.00\n")
    run = FakeRun(warm=states)
    run_main(tmp_path, proc, db, run, minutes="10", warm=warm)
    assert not run.powered_off
    (proc / "uptime").write_text(f"{idle.WARM_MAX_S}.00 1.00\n")
    run = FakeRun(warm=states)
    assert run_main(tmp_path, proc, db, run, minutes="10", warm=warm) == 0
    assert run.powered_off
    last = (tmp_path / "idle.log").read_text().splitlines()[-1]
    assert (
        last.endswith("(ignoring the first boot's volume warm-up, unfinished after 3 h up)")
        and " power off: idle for 10 min " in last
    ), last


def test_a_systemctl_that_cant_run_holds_while_the_marker_is_missing(tmp_path, proc, db):
    def run(argv, **kwargs):
        if argv == idle.WARM_ACTIVE:
            raise subprocess.TimeoutExpired(argv, 30)
        return FakeRun()(argv, **kwargs)

    got = gather(tmp_path, proc, db, run, warm=str(tmp_path / "none"))
    assert got.warming is None
    assert "the first boot's volume warm-up unknown" in idle.hold_reasons(got, 10, 0.5)


# ------------------------------------------------------------------ a spare's (#715)


def test_admin_tomls_spare_idle_stop_has_idle_stops_bounds():
    """`spare_idle_stop`'s bounds and default (zab.admin.config) are idle stop's own."""
    from zab.admin.config import IDLE_STOP_MINUTES, SPARE_IDLE_STOP

    assert IDLE_STOP_MINUTES[0] == idle.MIN_MINUTES <= SPARE_IDLE_STOP


def test_a_spare_without_zabs_database_powers_off_when_idle(tmp_path, proc):
    """#715: a spare has no bridge, so no database; with `--spare` that holds nothing, and its
    verdicts go to the spare's log, never to the participant's idle stop's."""
    none = tmp_path / "none.db"
    got = gather(tmp_path, proc, none, FakeRun(), spare=True)
    assert got.running_turns == 0 and not got.errors and got.last_activity is None
    assert idle.hold_reasons(got, 10, 0.5) == []
    run, spare_log = FakeRun(), tmp_path / "spare.log"
    done = run_main(tmp_path, proc, none, run, "--spare", minutes="10", spare_log=str(spare_log))
    assert done == 0 and run.powered_off
    assert "power off: idle for 10 min" in spare_log.read_text()
    assert not (tmp_path / "idle.log").exists()
    # without --spare, the same VM stays up: its database is missing (PM on #218)
    run = FakeRun()
    run_main(tmp_path, proc, none, run, minutes="10")
    assert not run.powered_off
    assert "cannot read zab's database: missing" in (tmp_path / "idle.log").read_text()


@pytest.mark.parametrize(
    ("changes", "who", "why"),
    [
        ({"uptime": "300.00 100.00\n"}, "", "up only 5 min"),
        ({"loadavg": "3.00 2.10 1.00 4/300 4242\n"}, "", "load 2.10"),
        ({}, "ubuntu pts/0 2026-10-11 12:00\n", "logged in: ubuntu@pts/0"),
    ],
)
def test_a_spare_keeps_idle_stops_other_rules(tmp_path, proc, changes, who, why):
    """#715's rules are idle stop's own: its first N minutes, the load, a login."""
    for name, text in changes.items():
        (proc / name).write_text(text)
    run, spare_log = FakeRun(who=who), tmp_path / "spare.log"
    none = tmp_path / "none.db"
    run_main(tmp_path, proc, none, run, "--spare", minutes="10", spare_log=str(spare_log))
    assert not run.powered_off
    assert why in spare_log.read_text()


def test_a_spare_keeps_the_hold(tmp_path, proc):
    """The hold push_config.py writes covers a spare being adopted (#715)."""
    (tmp_path / "hold").write_text(f"{NOW + 600:.0f}\n")
    run, spare_log = FakeRun(), tmp_path / "spare.log"
    none = tmp_path / "none.db"
    run_main(tmp_path, proc, none, run, "--spare", minutes="10", spare_log=str(spare_log))
    assert not run.powered_off
    assert "held by a rollout until" in spare_log.read_text()
