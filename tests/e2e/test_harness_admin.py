"""The slow scenarios' helpers, offline (issue #263): push_config for the rig, the Zulip block."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import time

import pytest

from e2e import test_scenarios_slow as slow
from e2e.harness import admin, cleanup, config
from e2e.harness.vm import Result


def test_push_config_targets_the_rig_only_with_its_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ZAB_E2E_ADMIN_DIR", raising=False)
    cfg = config.load({"HOME": str(tmp_path), "ZAB_E2E_OUT_DIR": str(tmp_path / "out")})
    seen = []
    admin.push_config(
        cfg, "--idle-stop", "10", run=lambda a, e, t: seen.append((a, e)) or Result(0, "", "")
    )
    argv, env = seen[0]
    assert argv[1] == str(config.CHECKOUT / "workshop" / "admin" / "push_config.py")
    assert argv[argv.index("--only") + 1] == "zab-test" and argv[-2:] == ["--idle-stop", "10"]
    assert argv[argv.index("--known-hosts") + 1] == str(cfg.known_hosts)
    assert env["ZAB_SSH_IDENTITY"] == str(cfg.ssh_key)
    with pytest.raises(RuntimeError, match="exit 1: refused"):
        admin.push_config(cfg, run=lambda a, e, t: Result(1, "", "a\nrefused"))
    # the tested build's scripts when run.py names them (codex on #285)
    monkeypatch.setenv("ZAB_E2E_ADMIN_DIR", str(tmp_path / "tree" / "workshop" / "admin"))
    admin.push_config(cfg, run=lambda a, e, t: seen.append((a, e)) or Result(0, "", ""))
    assert seen[-1][0][1] == str(tmp_path / "tree" / "workshop" / "admin" / "push_config.py")


REFUSED = "idle stop off: ssh: connect to host 198.51.100.4 port 22: Connection refused"


def table(monkeypatch) -> str:
    """push_config's own table (`render`) for a run whose zab-test row failed."""
    monkeypatch.syspath_prepend(str(config.CHECKOUT / "workshop" / "admin"))
    pc = importlib.import_module("push_config")
    ok = pc.VmResult("alice", "203.0.113.5", installed=True, doctor_ok=True, notes=["note one"])
    failed = pc.VmResult("zab-test", "198.51.100.4", failures=[REFUSED])
    return pc.render([ok, failed], steps_only=True)


def test_failures_reads_the_rows_failures_cell(monkeypatch):
    printed = table(monkeypatch)
    assert admin.failures(printed, "zab-test") == REFUSED
    assert admin.failures(printed, "alice") == "note: note one"
    assert admin.failures(printed, "zab") == "" and admin.failures("", "zab-test") == ""


def test_a_failed_push_config_says_why_from_its_row(tmp_path, monkeypatch):
    """#517: the error was stderr's last line, `1 VM(s) failed`; now it is the row's failures,
    or the last line of the output when there is no row (a crash, a timeout)."""
    monkeypatch.delenv("ZAB_E2E_ADMIN_DIR", raising=False)
    cfg = config.load({"HOME": str(tmp_path), "ZAB_E2E_OUT_DIR": str(tmp_path / "out")})
    printed = Result(1, table(monkeypatch) + "\n", "\n1 VM(s) failed\n")
    with pytest.raises(RuntimeError) as caught:
        admin.push_config(cfg, "--idle-stop", "0", run=lambda a, e, t: printed)
    assert str(caught.value) == f"push_config --idle-stop 0: exit 1: {REFUSED}"
    crashed = Result(1, "", "Traceback …\nKeyError: 'ip'\n")
    with pytest.raises(RuntimeError, match=r"0: exit 1: KeyError: 'ip'$"):
        admin.push_config(cfg, "--idle-stop", "0", run=lambda a, e, t: crashed)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("script", [slow.BLOCK, cleanup.UNBLOCK])
def test_the_block_scripts_parse_and_take_inputs_as_arguments(script):
    subprocess.run(["bash", "-n", "-c", script], check=True)  # noqa: S603, S607
    assert "zab-e2e" in script and "%" not in script  # no systemd specifiers
    assert "/etc/hosts" not in slow.BLOCK  # rules in memory only: nothing survives a stop


def test_the_safety_timer_runs_unblock_unexpanded():
    """Reviewer on #285: systemd-run expands `${…}` in the command by default, and UNBLOCK's
    `"${rule[@]}"` became empty: the timer must pass the script through untouched."""
    argv = slow.safety_timer("zab-e2e-unblock-x", 900)
    flag = argv.index("--expand-environment=no")
    assert argv[:2] == ["sudo", "systemd-run"] and flag < argv.index("/bin/bash")
    assert argv[-3:] == ["/bin/bash", "-c", cleanup.UNBLOCK] and "${" in cleanup.UNBLOCK
    assert "--on-active=900" in argv and "--unit=zab-e2e-unblock-x" in argv


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_unblock_deletes_every_tagged_rule_as_iptables_prints_it(tmp_path):
    """The run-level clean-up needs no addresses: it reads the rules back (reviewer on #283)."""
    log = tmp_path / "calls"
    fake = tmp_path / "iptables"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {log}\n'
        'if [ "$1" = -S ]; then\n'
        "  echo '-P OUTPUT ACCEPT'\n"
        "  echo '-A OUTPUT -d 1.2.3.4/32 -p tcp -m comment --comment zab-e2e -j REJECT "
        "--reject-with tcp-reset'\n"
        "  echo '-A OUTPUT -d 5.6.7.8/32 -j ACCEPT'\n"
        "fi\n"
    )
    fake.chmod(0o755)
    (tmp_path / "ip6tables").symlink_to(fake)
    script = cleanup.UNBLOCK.replace("sed -i '/ # zab-e2e$/d' /etc/hosts", "true")
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    subprocess.run(["bash", "-c", script], check=True, env=env)  # noqa: S603, S607
    deleted = [line for line in log.read_text().splitlines() if line.startswith("-D")]
    assert (
        deleted
        == [
            "-D OUTPUT -d 1.2.3.4/32 -p tcp -m comment --comment zab-e2e -j REJECT --reject-with "
            "tcp-reset"
        ]
        * 2
    )  # iptables and ip6tables; the untagged rule stays


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_login_wait_returns_once_a_tick_recorded_the_session(tmp_path):
    """Codex on #285: the idle window starts only after idle stop has seen a login."""
    from e2e.test_idle_stop import WAIT_FOR_LOGIN

    seen = tmp_path / "login"
    script = WAIT_FOR_LOGIN.replace("/run/zab-idle-stop.login", str(seen)).replace(
        "sleep 5", "sleep 0.1"
    )
    script = script.replace("seq 1 36", "seq 1 5")
    failed = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert failed.returncode == 1 and "no login" in failed.stderr  # never touched: gives up
    # Each tick lands between two polls, whatever the load (#545): a poll that finds no login
    # prints the start and waits for the test instead of sleeping. The test stamps the file
    # itself, as a touch's clock can lag `date` into the second before. A login stamped before
    # the start must not count; one in the start's second, or the next, must (review on #550).
    gated = script.replace("sleep 0.1", 'echo "$start"; read -r _')
    for later in (0, 1):
        seen.unlink(missing_ok=True)
        before = int(time.time())
        with subprocess.Popen(  # noqa: S603
            ["bash", "-c", gated],  # noqa: S607
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as waiting:
            line = waiting.stdout.readline()  # the first poll found no login
            start = int(line)
            assert before <= start <= time.time()  # the start is the clock's, not the past's
            seen.touch()
            os.utime(seen, (start - 1, start - 1))  # an earlier tick's login doesn't count
            waiting.stdin.write("\n")
            waiting.stdin.flush()
            assert waiting.stdout.readline() == line  # the second poll found no login either
            os.utime(seen, (start + later, start + later))
            waiting.communicate("\n")
        assert waiting.returncode == 0


def test_only_this_runs_power_off_line_counts():
    from e2e.test_idle_stop import this_runs_power_offs

    log = (
        "2026-10-12T09:00:00Z power off: idle for 10 min (an earlier run)\n"
        "2026-10-13T18:00:00Z stay up: a turn is running\n"
        "2026-10-13T18:11:00Z power off: idle for 10 min (no turn, no login, load 0.01)\n"
    )
    since = 1791914400  # 2026-10-13T18:00:00Z
    found = this_runs_power_offs(log, since, since + 900)
    assert found == [log.splitlines()[2]]
    assert this_runs_power_offs(log, since + 1000, since + 2000) == []
