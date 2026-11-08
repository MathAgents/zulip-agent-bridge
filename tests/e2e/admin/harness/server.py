"""SSH to the test admin server, as `ubuntu` with sudo (docs/ADMIN.md §7; issue #356).

The same pinned, forward-nothing connection as the #263 VM's (`harness/vm.Vm`): argument lists only,
joined with `shlex.join`; a step that needs a pipeline or a loop is a fixed literal of this
repository with its inputs as positional arguments (CONTRIBUTING.md, rule 3). Some steps also send
data on standard input (a file to install, the texts the secret sweep scans), never as a command.
Nothing a remote command prints is executed. A command whose connection failed before it ran is
tried again, within its timeout, as the VM's are (`Vm.call`, #517 item 4).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from e2e.admin.harness.rig import WORKSHOPS, AdminRig
from e2e.harness.vm import CONNECT_TRIES, RemoteError, Result, Vm, say

BOT = "zab-admin-bot.service"
#: deploy.sh's payload directories on the server, `mktemp -d /tmp/zab-deploy.XXXXXXXXXX` run as
#: `ubuntu`, for `find`
DEPLOY_DIRS = ("/tmp", "-maxdepth", "1", "-type", "d", "-user", "ubuntu", "-name",  # noqa: S108
               "zab-deploy.??????????")  # fmt: skip
VAR = "/var/lib/zab-admin"
CURRENT = "/opt/zab-admin/current"
#: The test workshops' state, emptied before a run: each starts from no roster (A2).
RESET = r"""set -eu
for ws in "$@"; do
  case $ws in zab-admin-e2e|zab-admin-e2e2) ;; *) echo "refused: $ws" >&2; exit 2 ;; esac
  rm -rf -- "/var/lib/zab-admin/ws/$ws"
  install -d -m 0700 -o zab-admin -g zab-admin "/var/lib/zab-admin/ws/$ws" \
    "/var/lib/zab-admin/ws/$ws/out"
done
"""
#: The file of /etc/zab-admin named $1 (admin.toml, allowed_signers, backup_recipients), from
#: standard input, root 0644, written atomically.
INSTALL_ETC = r"""set -eu
case $1 in admin.toml|allowed_signers|backup_recipients) ;; *) exit 2 ;; esac
install -m 0644 -o root -g root /dev/stdin "/etc/zab-admin/$1.new"
mv -f -- "/etc/zab-admin/$1.new" "/etc/zab-admin/$1"
"""

#: The secret named $1 (Q9's two only: the others come with the bootstrap), from standard
#: input, root:zab-admin 0640 as deploy.sh installs it; the bot restarts after it.
INSTALL_SECRET = r"""set -eu
case $1 in github-token|provisioner.zuliprc) ;; *) exit 2 ;; esac
install -m 0640 -o root -g zab-admin /dev/stdin "/etc/zab-admin/secrets/$1.new"
mv -f -- "/etc/zab-admin/secrets/$1.new" "/etc/zab-admin/secrets/$1"
"""  # noqa: S105 - a script, not a password


def ssh_runner(argv: list[str], timeout: float, input: str | None = None) -> Result:
    try:
        done = subprocess.run(  # noqa: S603 - an argument list, never a shell string
            argv, input=input, capture_output=True, text=True, timeout=timeout, check=False,
            stdin=None if input is not None else subprocess.DEVNULL,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout:.0f} s")
    return Result(done.returncode, done.stdout, done.stderr)


class Server:
    def __init__(
        self, ip: str, rig: AdminRig, runner=ssh_runner, log: Callable[[str], None] = say
    ) -> None:
        self.vm = Vm(ip, key=rig.ssh_key, known_hosts=rig.known_hosts, log=log)
        self.ip, self.runner = ip, runner

    def run(
        self,
        remote: Sequence[str],
        *,
        input: str | None = None,
        timeout: float = 120,
        check: bool = True,
        tries: int = CONNECT_TRIES,
    ) -> Result:
        def send(argv: list[str], seconds: float) -> Result:
            return self.runner(argv, seconds, input)  # again: the command never read it

        result = self.vm.call(remote, timeout, send, tries=tries)
        if check and not result.ok:
            raise RemoteError(remote, result)
        return result

    def script(
        self, literal: str, *args: str, sudo: bool = False, input: str | None = None, **kw
    ) -> Result:
        """A fixed script of this repository, with `args` as `$1…`, never spliced into it."""
        argv = [*(["sudo"] if sudo else []), "bash", "-c", literal, "bash", *args]
        return self.run(argv, input=input, **kw)

    def probe(self) -> Result:
        """One connection that runs `true`, never retried: its callers poll it, with their own
        pauses (`startup.steady_ssh`), and its stderr says why it failed."""
        return self.run(["true"], timeout=30, check=False, tries=1)

    def reachable(self) -> bool:
        return self.probe().ok

    def sweep_deploy_dirs(self) -> list[str]:
        """deploy.sh's payload directories left in /tmp, removed; their names. deploy.sh makes
        one with `mktemp -d /tmp/zab-deploy.XXXXXXXXXX` as `ubuntu`, and its exit trap removes
        it only if it can still connect: after a connect failed, it may stay, and a
        bootstrap's holds the rig's etc/ and secrets/ (#804, review N2)."""
        found = self.run(["sudo", "find", *DEPLOY_DIRS, "-print", "-exec", "rm", "-rf", "--",
                          "{}", "+"])  # fmt: skip
        return found.stdout.split()

    def running(self) -> str | None:
        """The release `current` points to (`v0.2.4rc1`), or None before the bootstrap."""
        found = self.run(["readlink", CURRENT], check=False)
        return Path(found.stdout.strip()).name if found.ok and found.stdout.strip() else None

    def bot(self, action: str) -> Result:
        if action not in ("start", "stop", "restart", "is-active"):
            raise ValueError(f"systemctl {action}")
        return self.run(["sudo", "systemctl", action, BOT], check=False, timeout=180)

    def reset_workshops(self) -> None:
        self.script(RESET, *(ws.name for ws in WORKSHOPS), sudo=True)

    def install_etc(self, name: str, text: str) -> None:
        self.script(INSTALL_ETC, name, sudo=True, input=text)

    def install_secret(self, name: str, text: str) -> None:
        self.script(INSTALL_SECRET, name, sudo=True, input=text)

    def secret_sha256(self, name: str) -> str:
        found = self.run(["sudo", "sha256sum", "--", f"/etc/zab-admin/secrets/{name}"], check=False)
        return found.stdout.split()[0] if found.ok and found.stdout.split() else ""

    def read(self, path: str) -> str:
        """A file only root may read (audit.jsonl, a roster): its text."""
        return self.run(["sudo", "cat", "--", path], timeout=300).stdout

    def as_admin(self, *argv: str, timeout: float = 300, input: str | None = None) -> Result:
        """`argv` as `zab-admin`, with a clean environment, as its units run (§2.3)."""
        env = ["env", "-i", "PATH=/usr/bin:/bin", f"HOME={VAR}", "LANG=C.UTF-8"]
        return self.run(
            ["sudo", "-u", "zab-admin", *env, *argv], timeout=timeout, input=input, check=False
        )

    def as_user(
        self, user: str, *argv: str, group: str | None = None, timeout: float = 300,
        input: str | None = None,
    ) -> Result:  # fmt: skip
        """`argv` as `user`, with a clean environment: with its own groups, or with `group` as
        its only one (`setpriv`), as no unit runs it (A19: `zab-waker` in `zab-admin`'s group)."""
        if group is None:
            become = ["sudo", "-u", user]
        else:
            become = ["sudo", "setpriv", f"--reuid={user}", f"--regid={group}", "--clear-groups"]
            become.append("--")
        env = ["env", "-i", "PATH=/usr/bin:/bin", "HOME=/", "LANG=C.UTF-8"]
        return self.run([*become, *env, *argv], timeout=timeout, input=input, check=False)
