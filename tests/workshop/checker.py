"""`zab-archive-check.socket` as the tests have it: systemd's `Accept=yes`, emulated. Each
connection starts the checker with the connection as fd 3 and `LISTEN_FDS`/`LISTEN_PID` set,
as systemd starts an instance of `zab-archive-check@.service`, with an empty environment and
`/` as its working directory. Not the sandbox: the unit's tests check that."""

from __future__ import annotations

import os
import pathlib
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

ARCHIVE_CHECK = pathlib.Path(__file__).resolve().parents[2] / "workshop/admin/archive_check.py"
#: The unit's own command line, the interpreter aside (zab-archive-check@.service).
SERVE = ["-I", "-S", "-B", str(ARCHIVE_CHECK), "serve"]
#: What systemd does between accept() and exec: the connection at fd 3, and the two variables.
LAUNCH = (
    "import os, sys\n"
    "fd = int(sys.argv[1])\n"
    "if fd != 3:\n"
    "    os.dup2(fd, 3)\n"
    "    os.close(fd)\n"
    "os.environ.update(LISTEN_FDS='1', LISTEN_PID=str(os.getpid()))\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)


def me() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def instance_slice(name: str) -> str:
    """The slice a user manager makes for the instances of `name@.service`, which stays active
    once they end: a test that leaves it adds a unit to the manager for good. By Oct 23, 4,248
    of them on the development machine made each `systemctl --user` call take 4 to 13 s, past
    `INSPECT`'s 8 s of waiting for an instance (#609)."""
    return "app-" + name.replace("-", "\\x2d") + ".slice"


class Checker:
    """A listening socket at `path`, short enough for AF_UNIX, whose every connection starts
    `argv` (by default the checker, answering this test's user). `children` are the processes
    started, in order."""

    def __init__(self, argv: list[str] | None = None) -> None:
        self.argv = argv or [sys.executable, *SERVE, "--peer", me()]
        self.dir = tempfile.mkdtemp(prefix="zab-ac-")
        self.path = os.path.join(self.dir, "check.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen()
        self.children: list[subprocess.Popen] = []
        self.closing = False
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while True:
            conn, _ = self.sock.accept()
            with conn:
                if self.closing:
                    return
                argv = [sys.executable, "-I", "-S", "-c", LAUNCH, str(conn.fileno()), *self.argv]
                self.children.append(
                    subprocess.Popen(argv, pass_fds=[conn.fileno()], env={}, cwd="/")  # noqa: S603
                )

    def close(self) -> None:
        self.closing = True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
            wake.connect(self.path)
        self.thread.join(timeout=5)
        self.sock.close()
        for child in self.children:  # each has answered by now; a hung fake one is stopped
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        shutil.rmtree(self.dir, ignore_errors=True)
