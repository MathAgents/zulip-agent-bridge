"""LocalWorkspace: file and command access to a workspace on this machine (SPEC §10).

This is the Phase 1 implementation of the ``Workspace`` protocol in ``zab.core.types``.
The bridge and the agent run as the same Unix user, so files are read and written
directly and commands are spawned as children (SPEC §12.2, DECISIONS D9).

Interpretation of SPEC §10.6: the contract's ``Workspace.run`` has no environment
parameter, so ``LocalWorkspace.run`` sets ``GIT_TERMINAL_PROMPT=0`` and
``GIT_OPTIONAL_LOCKS=0`` in the child environment of *every* command it runs. Both are
harmless for non-git commands and guarantee that no git call can block on a credential
prompt or take an optional lock. Inherited ``GIT_*`` variables are dropped (issue #36),
with an allowlist for the identity variables ``GIT_AUTHOR_NAME``, ``GIT_AUTHOR_EMAIL``,
``GIT_AUTHOR_DATE``, ``GIT_COMMITTER_NAME``, ``GIT_COMMITTER_EMAIL`` and
``GIT_COMMITTER_DATE``: they only name the identity git records in the throwaway commit of
``git stash create`` (§10.5) and cannot redirect a repository, its configuration, its
objects or its helpers. Everything else is removed: ``GIT_DIR``, ``GIT_WORK_TREE``,
``GIT_INDEX_FILE``, ``GIT_OBJECT_DIRECTORY``, ``GIT_ALTERNATE_OBJECT_DIRECTORIES``,
``GIT_CONFIG*``, ``GIT_CEILING_DIRECTORIES``, ``GIT_NAMESPACE``, ``GIT_EXEC_PATH``,
``GIT_SSH*``, ``GIT_PROXY_COMMAND`` and any other ``GIT_*`` name, since each can point the
bridge's git calls at another repository, configuration or program. (On git 2.43 ``git stash
create`` falls back to a guessed identity even under ``user.useConfigOnly``; the allowlist
keeps an operator's chosen identity on the stash commits and guards other git versions.)
Commands are always argument vectors; nothing here ever passes text to a shell (SPEC
§10.6, §12.1; CONTRIBUTING.md, rule 3).
"""

from __future__ import annotations

import asyncio
import os
import signal
import stat as statmod
from collections.abc import Sequence

from zab.core.types import CommandResult, FileStat

_ENV_OVERRIDES = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}

#: The only inherited ``GIT_*`` variables passed on to children (see the module docstring).
GIT_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
    }
)


class LocalWorkspace:
    """A workspace on the local filesystem (SPEC §10).

    ``root`` is the repository or worktree root and ``cwd`` is where the agent runs
    (``root`` plus the optional subdir, SPEC §10.1). Relative paths given to the methods
    resolve against ``cwd``.
    """

    def __init__(self, root: str, cwd: str | None = None) -> None:
        self._root = os.path.abspath(os.path.expanduser(root))
        self._cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else self._root

    @property
    def root(self) -> str:
        return self._root

    @property
    def cwd(self) -> str:
        return self._cwd

    def __repr__(self) -> str:
        return f"LocalWorkspace(root={self._root!r}, cwd={self._cwd!r})"

    def _resolve(self, path: str) -> str:
        """Absolute form of ``path``; relative paths are taken against ``cwd``.

        No ``normpath``: ``..`` components stay in place so that the filesystem (and
        ``os.path.realpath``) resolve symlinks first, as the agent's own tools do.
        """
        return os.path.join(self._cwd, os.path.expanduser(path))

    async def realpath(self, path: str) -> str:
        """Canonical absolute path with symlinks resolved; the path need not exist.

        A path that the OS cannot even look at (an embedded NUL byte) is returned
        unresolved, so callers see a path that ``stat`` then reports as missing.
        """
        target = self._resolve(path)  # already absolute: cwd is
        try:
            return await asyncio.to_thread(os.path.realpath, target)
        except ValueError:
            return target

    async def stat(self, path: str) -> FileStat | None:
        """Size, kind and mtime of ``path``, or ``None`` if it cannot be stat'ed.

        Any ``OSError`` counts as "not there" (missing, not a directory, a name longer than
        the OS allows such as a 300-character token or a ``data:`` URI from agent text, a
        permission error, a symlink loop), as does an embedded NUL byte (``ValueError``).
        The renderer must degrade on such paths, not raise (issue #36).
        """

        def _stat() -> FileStat | None:
            try:
                st = os.stat(self._resolve(path))
            except (OSError, ValueError):
                return None
            return FileStat(size=st.st_size, is_dir=statmod.S_ISDIR(st.st_mode), mtime=st.st_mtime)

        return await asyncio.to_thread(_stat)

    async def read_bytes(self, path: str, max_bytes: int) -> bytes:
        """The first ``max_bytes`` bytes of the file at ``path``."""

        def _read() -> bytes:
            with open(self._resolve(path), "rb") as f:
                return f.read(max(0, max_bytes))

        return await asyncio.to_thread(_read)

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path``, creating parent directories as needed."""

        def _write() -> None:
            target = self._resolve(path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(data)

        await asyncio.to_thread(_write)

    async def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 10.0
    ) -> CommandResult:
        """Run a fixed argument vector; never a shell string (SPEC §10.6).

        stdin is closed, stdout and stderr are captured, and the child gets a copy of the
        environment without inherited ``GIT_*`` variables other than the identity ones in
        ``GIT_ENV_ALLOWLIST``, plus ``GIT_TERMINAL_PROMPT=0`` and ``GIT_OPTIONAL_LOCKS=0``.
        The command runs in its own session and process group;
        when it runs longer than ``timeout_s``, or the awaiting task is cancelled, the whole
        group is killed (descendants included, so nothing keeps the pipes open) and reaped
        before ``TimeoutError`` is raised or the cancellation is re-raised.
        """
        args = [str(a) for a in argv]
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GIT_") or k in GIT_ENV_ALLOWLIST
        }
        env.update(_ENV_OVERRIDES)
        proc = await asyncio.create_subprocess_exec(  # argv list, never a shell (§10.6)
            *args,
            cwd=self._resolve(cwd) if cwd else self._cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the group id is the pid (new session)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TimeoutError(f"command timed out after {timeout_s}s: {args}") from None
        return CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )
