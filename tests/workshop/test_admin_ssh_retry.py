"""`admin_common.ConnectRetry` (#1009): an SSH connect that fails is tried again, after 5 s and
then 15 s, only to a VM that answered earlier in the run, and only when nothing ran."""

from __future__ import annotations

import datetime
import re

import admin_common as ac
import pytest

HOST = "198.51.100.7"
OTHER = "198.51.100.9"
KH = "/p/known_hosts"
#: What OpenSSH 9.6p1 printed here for a closed port and for TEST-NET-1 (CRLF: ssh's log line).
TIMED_OUT = f"ssh: connect to host {HOST} port 22: Connection timed out\r\n"
REFUSED = f"ssh: connect to host {HOST} port 22: Connection refused\r\n"
#: A connect-phase failure: exit 255, nothing on stdout, ssh's connect line for the host alone.
CONNECT_FAILED = {
    "timed out": TIMED_OUT,
    "refused": REFUSED,
    "no route": f"ssh: connect to host {HOST} port 22: No route to host\n",
    "timed out, macOS": f"ssh: connect to host {HOST} port 22: Operation timed out\n",
}
#: Exit 255 that may come after the connection was up, or isn't a TCP connect's failure.
NOT_CONNECT_FAILED = {
    "session closed": f"Connection to {HOST} closed by remote host.\r\n",
    "session read": f"Read from remote host {HOST}: Connection reset by peer\r\n",
    "broken pipe": "client_loop: send disconnect: Broken pipe\r\n",
    "bare closed": f"Connection closed by {HOST} port 22\r\n",
    "auth": f"ubuntu@{HOST}: Permission denied (publickey).\r\n",
    "host key": "Host key verification failed.\r\n",
    "banner timed out": "Connection timed out during banner exchange\r\n"
    f"Connection to {HOST} port 22 timed out\r\n",
    "the command's own 255": "",
    "another host": TIMED_OUT.replace(HOST, OTHER),
    "network unreachable": f"ssh: connect to host {HOST} port 22: Network is unreachable\n",
    "the command's stderr, then the line": "mktemp: failed\n" + TIMED_OUT,
    "the line, then more": TIMED_OUT + "bash: line 1: oops\n",
    "the line with more on it": TIMED_OUT.rstrip() + " again\n",
}
ADOPT = "sudo tee /etc/zab-participant.json >/dev/null && sudo hostnamectl set-hostname ws-x"


def ssh(remote: str = "true", host: str = HOST) -> list[str]:
    return ac.ssh_argv(host, remote, known_hosts=KH)


def scp(host: str = HOST) -> list[str]:
    return ac.scp_argv("/tmp/config.toml", host, "/tmp/zab-push.x/config.toml", known_hosts=KH)  # noqa: S108


class Scripted:
    """A fake runner: each call takes the next of `answers` (exit, stderr), else `(0, "")`."""

    def __init__(self, *answers: tuple[int, str]):
        self.answers = list(answers)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        code, stderr = self.answers.pop(0) if self.answers else (0, "")
        return ac.RunResult(code, "out\n" if code != 255 else "", stderr)


class Clock:
    """`now` for the runner: 09:30:00Z, then 20 s later at each call."""

    def __init__(self):
        self.t = datetime.datetime(2026, 11, 9, 9, 30, tzinfo=datetime.UTC)

    def __call__(self):
        t, self.t = self.t, self.t + datetime.timedelta(seconds=20)
        return t


def retrying(fake: Scripted, **kw) -> tuple[ac.ConnectRetry, list[float]]:
    slept: list[float] = []
    return ac.ConnectRetry(fake, sleep=slept.append, now=Clock(), **kw), slept


# ------------------------------------------------------------------------------- the classifier


@pytest.mark.parametrize("stderr", CONNECT_FAILED.values(), ids=CONNECT_FAILED.keys())
def test_a_tcp_connect_that_failed_is_recognized(stderr):
    why = stderr.split(": ")[-1].strip()
    assert ac.connect_failure(ssh(), ac.RunResult(255, "", stderr)) == why


@pytest.mark.parametrize("stderr", NOT_CONNECT_FAILED.values(), ids=NOT_CONNECT_FAILED.keys())
def test_a_failure_that_may_follow_the_command_is_not_a_connect_failure(stderr):
    assert ac.connect_failure(ssh(), ac.RunResult(255, "", stderr)) == ""


def test_only_ssh_s_own_exit_with_nothing_on_stdout_counts():
    assert ac.connect_failure(ssh(), ac.RunResult(255, "", TIMED_OUT))
    assert not ac.connect_failure(ssh(), ac.RunResult(255, "started\n", TIMED_OUT))
    for code in (-1, 0, 1, 254):  # the runner's timeout, or the remote command's own exit
        assert not ac.connect_failure(ssh(), ac.RunResult(code, "", TIMED_OUT))
    for argv in (["git", "push"], ["ssh-keyscan", "--", HOST], ["ssh", f"ubuntu@{HOST}"]):
        assert not ac.connect_failure(argv, ac.RunResult(255, "", TIMED_OUT)), argv


def test_scp_s_failed_connect_is_recognized_with_its_closing_line():
    """OpenSSH 9.6p1's scp, exit 255: its ssh's line, then its own."""
    closed = REFUSED + "scp: Connection closed\r\n"
    assert ac.connect_failure(scp(), ac.RunResult(255, "", closed)) == "Connection refused"
    assert ac.connect_failure(scp(), ac.RunResult(255, "", TIMED_OUT)) == "Connection timed out"
    lost = REFUSED + "scp: dest open: Permission denied\r\n"
    assert not ac.connect_failure(scp(), ac.RunResult(255, "", lost))
    assert not ac.connect_failure(scp(OTHER), ac.RunResult(255, "", closed))
    assert not ac.connect_failure(ssh(), ac.RunResult(255, "", closed))  # ssh has no such line


def test_ssh_host_reads_only_the_command_lines_the_scripts_build():
    assert ac.ssh_host(ssh("a -- b")) == HOST
    assert ac.ssh_host(scp()) == HOST
    assert ac.ssh_host(["ssh", f"ubuntu@{HOST}", "--", "a", "b"]) == ""  # not one remote word
    assert ac.ssh_host(["ssh", HOST, "--", "true"]) == ""  # no user: not ssh_argv's
    assert ac.ssh_host(["scp", "--", "/a", f"ubuntu@{HOST}:/b"]) == HOST
    assert ac.ssh_host(["git", "-C", "/x", "push"]) == ""


# -------------------------------------------------------------------------------- the retries


def test_a_connect_failure_after_an_answer_is_retried_after_5_s():
    fake = Scripted((0, ""), (255, TIMED_OUT), (0, ""))
    run, slept = retrying(fake)
    assert run(ssh("whoami"), timeout_s=60).note == ""
    res = run(ssh("mktemp"), timeout_s=60, stdin="x")
    assert res.ok and slept == [5.0]
    assert res.note == "ssh: connected on try 2 (try 1 began 09:30:20Z: Connection timed out)"
    assert [c[0][-1] for c in fake.calls] == ["whoami", "mktemp", "mktemp"]
    assert fake.calls[2][1] == {"timeout_s": 60, "stdin": "x"}  # passed on as given


def test_two_retries_wait_5_s_then_15_s_and_then_it_fails():
    fake = Scripted((1, ""), (255, TIMED_OUT), (255, REFUSED), (0, ""))
    run, slept = retrying(fake)
    run(ssh())  # exit 1: the command ran, so the VM answered
    res = run(ssh())
    assert res.ok and slept == [5.0, 15.0]
    assert res.note == (
        "ssh: connected on try 3 (tries 1-2 began 09:30:20Z, 09:30:40Z: "
        "Connection timed out / Connection refused)"
    )
    fake = Scripted((0, ""), *[(255, TIMED_OUT)] * 4)
    run, slept = retrying(fake)
    run(ssh())
    res = run(ssh("ls"))
    assert (res.exit_code, slept, res.note) == (255, [5.0, 15.0], "")
    assert res.stderr == TIMED_OUT.replace("\r\n", " (3 tries from 09:30:20Z)\r\n")
    assert len(fake.calls) == 4
    # gone longer than the waits: the next call fails at once, until the VM answers again
    res = run(ssh("ls"))
    assert res.stderr == TIMED_OUT.replace("\r\n", " (began 09:31:20Z)\r\n")
    assert (len(fake.calls), slept) == (5, [5.0, 15.0])


def test_a_vm_that_never_answered_fails_at_its_first_try_as_before():
    """Criterion 3: a stopped VM, or a wrong address, gets no wait; its line says when."""
    for stderr in CONNECT_FAILED.values():
        fake = Scripted((255, stderr))
        run, slept = retrying(fake)
        res = run(ssh(), timeout_s=60)
        assert (res.exit_code, res.stdout, len(fake.calls), slept) == (255, "", 1, [])
        line = stderr.splitlines()[0]
        assert res.stderr == stderr.replace(line, f"{line} (began 09:30:00Z)")
    # nor does another VM's answer count, nor an answer that only shows ssh reached sshd
    fake = Scripted((0, ""), (255, f"ubuntu@{HOST}: Permission denied (publickey).\r\n"))
    run, slept = retrying(fake)
    run(ssh(host=OTHER))
    run(ssh())
    fake.answers = [(255, TIMED_OUT)]
    assert run(ssh()).exit_code == 255 and slept == [] and len(fake.calls) == 3


@pytest.mark.parametrize("stderr", NOT_CONNECT_FAILED.values(), ids=NOT_CONNECT_FAILED.keys())
def test_a_failure_once_connected_is_never_retried(stderr):
    """Criterion 2: `ADOPT_CMD` and others aren't idempotent. An exit 255 after the connection
    was up, a cut mid-command among them, is returned as it came, at the first try."""
    fake = Scripted((0, ""), (255, stderr))
    run, slept = retrying(fake)
    run(ssh())
    res = run(ssh(ADOPT), timeout_s=60, stdin="{}")
    assert res == ac.RunResult(255, "", stderr)
    assert slept == [] and [c[0][-1] for c in fake.calls].count(ADOPT) == 1


def test_scp_is_retried_alike_and_counts_as_an_answer():
    closed = TIMED_OUT + "scp: Connection closed\r\n"
    fake = Scripted((0, ""), (255, closed), (0, ""), (255, TIMED_OUT), (0, ""))
    run, slept = retrying(fake)
    run(scp())
    res = run(scp())
    assert res.ok and res.note.startswith("scp: connected on try 2 (try 1 began ") and slept == [5]
    assert run(ssh()).note.startswith("ssh: connected on try 2 ") and slept == [5, 5]
    fake = Scripted((1, "scp: /tmp/x: No such file"), (255, closed))  # scp's 1: maybe not there
    run, slept = retrying(fake)
    run(scp())
    assert run(scp()).exit_code == 255 and slept == []


def test_other_commands_pass_through_and_the_note_is_logged():
    fake = Scripted((255, TIMED_OUT), (0, ""), (255, TIMED_OUT))
    said: list[str] = []
    run, slept = retrying(fake, log=said.append)
    res = run(["git", "push"], timeout_s=30, env={"A": "1"})  # not ssh: as it came
    assert res == ac.RunResult(255, "", TIMED_OUT) and slept == []
    assert fake.calls[0][1] == {"timeout_s": 30, "env": {"A": "1"}}
    run(ssh())
    run(ssh())
    assert slept == [5.0] and said == [
        f"{HOST}: ssh: connected on try 2 (try 1 began 09:30:40Z: Connection timed out)"
    ]


def test_the_waits_are_the_constants_and_tests_never_sleep(monkeypatch):
    assert ac.CONNECT_RETRY_WAITS_S == (5.0, 15.0)
    run = ac.ConnectRetry(Scripted())
    assert run.waits == (5.0, 15.0) and run.sleep is ac.time.sleep
    monkeypatch.setattr(ac, "CONNECT_RETRY_WAITS_S", (0.0,))
    assert ac.ConnectRetry(Scripted()).waits == (0.0,)


def test_noting_puts_each_note_in_the_step_s_result():
    notes: list[str] = []
    fake = Scripted((0, ""), (255, TIMED_OUT), (0, ""))
    run = ac.noting(retrying(fake)[0], notes)
    run(ssh())
    run(ssh())
    assert len(notes) == 1 and re.fullmatch(r"ssh: connected on try 2 \(.*\)", notes[0])
