"""`zab share`: a link to some files or folders alone, for the agents (issue #710; SPEC §10.7).

    zab share PATH [PATH…]
    zab share --list
    zab share --revoke LINK|NAME

For agents: when the participant wants to share a file or a folder with someone, this makes the
link. It commits only the given paths, as they stand in the working tree (uncommitted changes
included), with no parent and a random 256-bit nonce in its message, so that no one can compute
the link from the files; pushes it to `refs/zab/shares/<name>` with the deploy key, as turn
snapshots are pushed; and opens the link once, which is when the file server copies the share,
checking that it serves it. The link then opens only what was shared, for anyone who has it, and
keeps working until it is revoked, even after the VM ends and after the repository is deleted.

The rules are a snapshot's: the SPEC §11.4 denylist, 20 MB a file and 50 MB in all, no symbolic
links, no submodules, nothing outside the repository. A path named that breaks one is refused
with its reason, and nothing is pushed; inside a folder, files that break one, and files that
`.gitignore` excludes, are left out and listed. `--list` shows this repository's shares on
GitHub; `--revoke` deletes one's ref there, then asks the file server to check it at once.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import shutil
import stat as statmod
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from zab.config import ConfigError, load_config
from zab.workspace import git as g
from zab.workspace.denylist import upload_denied_reason
from zab.workspace.local import LocalWorkspace

SHARE_REFS = "refs/zab/shares/"
#: How long `zab share` waits for the file server to serve its new share, and `--revoke` for it
#: to stop: within an agent's 50-second call.
WAIT_S = 45.0
NONCE_BYTES = 32
_SHA = re.compile(r"/([0-9a-f]{40})(?:/|$)")
#: What the file server never serves in a path's component (`workshop/files/server.py`,
#: `allowed`): a control character or a backslash.
_UNSERVED = re.compile(r"[\x00-\x1f\x7f\\]")
UNSERVED = "a name the file server never serves (a control character or a backslash)"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class ShareError(Exception):
    """A refusal or a failure, as one line for the agent."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Picked:
    named: list[str] = field(default_factory=list)  # the paths given, folders with a `/`
    files: list[str] = field(default_factory=list)  # what goes in, repository-relative
    left_out: list[tuple[str, str]] = field(default_factory=list)  # inside folders, and why
    size: int = 0


def candidate(top: str, rel: str) -> tuple[str | None, int]:
    """Why the file at `rel` can't be shared, or None; and its size. Every component of its
    path is checked itself, never trusting git's listing (the reviewer on #744)."""
    parts = rel.split("/")
    if any(_UNSERVED.search(part) for part in parts):
        return UNSERVED, 0
    if upload_denied_reason(rel):
        return "the denylist", 0
    here = top
    for part in parts[:-1]:
        here = os.path.join(here, part)
        if os.path.islink(here):
            return "inside a symbolic link", 0
    try:
        st = os.lstat(os.path.join(top, rel))
    except OSError:
        return "gone", 0
    if statmod.S_ISLNK(st.st_mode):
        return "a symbolic link", 0
    if not statmod.S_ISREG(st.st_mode):
        return "not a regular file", 0
    if st.st_nlink > 1:
        return "more than one hard link", 0
    if st.st_size > g.SNAPSHOT_FILE_CAP:
        return f"over {g.SNAPSHOT_FILE_CAP >> 20} MB", 0
    return None, st.st_size


def resolve(top: str, cwd: str, given: str) -> tuple[str, bool]:
    """`given` as a repository-relative path, and whether it is a folder; ShareError (code 2)
    for a path that can't be shared, with its reason."""

    def refuse(why: str) -> ShareError:
        return ShareError(f"{given!r} {why}: nothing was shared", 2)

    if not given or any(c in given for c in "\n\r\0"):
        raise refuse("is not a path")
    if any(_UNSERVED.search(part) for part in given.split("/")):
        raise refuse(f"has {UNSERVED}")
    # Each component as written, before `..` is collapsed (Codex round 2 on #744): with `link`
    # a symbolic link, `link/../x` names x beside the link's target, which normpath would turn
    # into the x beside the link.
    here = "/" if os.path.isabs(given) else cwd
    for part in given.split("/"):
        if part in ("", "."):
            continue
        here = os.path.dirname(here) if part == ".." else os.path.join(here, part)
        if os.path.islink(here):
            raise refuse("is a symbolic link, or inside one")
    full = os.path.normpath(os.path.join(cwd, given))
    rel = os.path.relpath(full, top)
    if rel == ".":
        raise refuse("is the whole repository: share files or folders of it")
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        raise refuse("is outside the repository")
    here, st = top, None
    for part in rel.split(os.sep):
        here = os.path.join(here, part)
        try:
            st = os.lstat(here)
        except OSError:
            raise refuse("doesn't exist") from None
        if statmod.S_ISLNK(st.st_mode):
            raise refuse("is a symbolic link, or inside one")
    folder = st is not None and statmod.S_ISDIR(st.st_mode)
    if folder and os.path.lexists(os.path.join(full, ".git")):
        raise refuse("is a git repository of its own (a submodule)")
    if upload_denied_reason(rel + "/x" if folder else rel):
        raise refuse("is on the denylist of files never shared (SPEC §11.4)")
    if not folder:
        why, _ = candidate(top, rel)
        if why:
            raise refuse(f"can't be shared: {why}")
    return rel.replace(os.sep, "/"), folder


async def pick(ws: LocalWorkspace, top: str, cwd: str, paths: list[str]) -> Picked:
    """What the share holds: each file named, and each folder's files that git sees (tracked,
    or untracked and not ignored), less those a snapshot would leave out."""
    picked, seen = Picked(), set()

    async def listed(*args: str) -> list[str]:
        res = await g.git(ws, "--literal-pathspecs", "ls-files", "-z", *args, cwd=top, timeout_s=60)
        if res.exit_code != 0:
            raise ShareError(f"git can't list the files: {res.stderr.strip()[:200]}")
        return [p for p in res.stdout.split("\0") if p]

    for given in paths:
        rel, folder = await asyncio.to_thread(resolve, top, cwd, given)
        picked.named.append(rel + "/" if folder else rel)
        if not folder:
            found = [rel]
        else:
            found = []
            for entry in await listed("-s", "--", rel):  # `<mode> <sha> <stage>\t<path>`
                meta, _, path = entry.partition("\t")
                if meta.split(" ", 1)[0] == "160000":
                    picked.left_out.append((path, "a submodule"))
                else:
                    found.append(path)
            for path in await listed("--others", "--exclude-standard", "--", rel):
                if path.endswith("/"):
                    picked.left_out.append((path, "a git repository of its own"))
                else:
                    found.append(path)
        kept = 0
        for path in found:
            if path in seen:
                kept += 1
                continue
            why, size = await asyncio.to_thread(candidate, top, path)
            if why == "gone":
                continue  # a tracked file deleted in the working tree
            if why:
                picked.left_out.append((path, why))
                continue
            seen.add(path)
            picked.files.append(path)
            picked.size += size
            kept += 1
        if not kept:
            raise ShareError(f"{given!r} has nothing that can be shared: nothing was shared", 2)
    if picked.size > g.SNAPSHOT_TOTAL_CAP:
        raise ShareError(
            f"that is {picked.size >> 20} MB in all, over the {g.SNAPSHOT_TOTAL_CAP >> 20} MB a "
            "share can hold: nothing was shared",
            2,
        )
    return picked


async def commit(ws: LocalWorkspace, top: str, picked: Picked, name: str) -> str:
    """The share's commit, holding exactly `picked.files`, under `refs/zab/shares/<name>`."""
    tmp = await asyncio.to_thread(tempfile.mkdtemp, prefix="zab-share-")
    try:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}

        async def run(*args: str, **kw: Any) -> str:
            res = await g.git(ws, "--literal-pathspecs", *args, cwd=top, timeout_s=60, **kw)
            if res.exit_code != 0:
                raise ShareError(f"git {args[0]} failed: {res.stderr.strip()[:200]}")
            return res.stdout

        spec = os.path.join(tmp, "paths")
        await asyncio.to_thread(g._write, spec, "\0".join(picked.files) + "\0")
        await run("read-tree", "--empty", env=env)
        await run("add", "-f", f"--pathspec-from-file={spec}", "--pathspec-file-nul", env=env)
        entries = [e for e in (await run("ls-files", "-s", "-z", env=env)).split("\0") if e]
        held = {e.split("\t", 1)[1]: e.split(" ", 1)[0] for e in entries}
        modes_ok = all(mode in ("100644", "100755") for mode in held.values())
        if set(held) != set(picked.files) or not modes_ok:
            raise ShareError("the files changed while they were being shared: try again")
        tree = (await run("write-tree", env=env)).strip()
        lines = "".join(f"Path: {path}\n" for path in picked.named)
        message = f"zab share {name}\n\n{lines}Nonce: {secrets.token_hex(NONCE_BYTES)}"
        sha = (await run("commit-tree", tree, "-m", message, env=g._SNAPSHOT_IDENTITY)).strip()
        await run("update-ref", SHARE_REFS + name, sha, "")
        return sha
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)


async def push(ws: LocalWorkspace, top: str, refspec: str) -> None:
    res = await g.git(
        ws, "push", "--quiet", "--no-verify", "origin", refspec, cwd=top,
        timeout_s=g.PUSH_TIMEOUT_S, env=g._PUSH_ENV,
    )  # fmt: skip
    if res.exit_code != 0:
        last = res.stderr.strip().splitlines()[-1][:200] if res.stderr.strip() else ""
        raise ShareError(f"the push to GitHub failed: {last}")


async def on_github(ws: LocalWorkspace, top: str, remote: str) -> dict[str, str]:
    """This repository's shares on GitHub: name → commit, read from `remote`, the push
    destination `_github_origin` validated, where the shares were pushed (Codex round 1 on
    #744: `ls-remote origin` reads the fetch URL, which may name another repository)."""
    res = await g.git(
        ws, "ls-remote", "--refs", remote, SHARE_REFS + "*", cwd=top, timeout_s=60,
        env=g._PUSH_ENV,
    )  # fmt: skip
    if res.exit_code != 0:
        raise ShareError(f"GitHub can't be read: {res.stderr.strip()[:200]}")
    pairs = (line.split("\t") for line in res.stdout.splitlines())
    return {ref.removeprefix(SHARE_REFS): sha for sha, ref in (p for p in pairs if len(p) == 2)}


def opens(http: Callable[..., int], method: str, url: str, want: frozenset[int]) -> int:
    """`url` asked with `method` until it answers one of `want`, or `WAIT_S` passes: the last
    status (0: no answer). The server fetches a new share from GitHub at its first request,
    and answers 404 or 503 until it has it."""
    deadline, status = time.monotonic() + WAIT_S, 0
    while True:
        try:
            status = http(method, url, timeout=min(40.0, max(1.0, deadline - time.monotonic())))
        except httpx.HTTPError:
            status = 0
        if status in want or time.monotonic() + 3 > deadline:
            return status
        time.sleep(3)


def http_status(method: str, url: str, timeout: float) -> int:
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        return client.request(method, url).status_code


def link(base: str, repo: str, sha: str, named: list[str]) -> str:
    """The file itself for one file, the folder for one folder, the share's root otherwise."""
    tail = quote(named[0], safe="/") if len(named) == 1 else ""
    return f"{base}/{quote(repo, safe='')}/{sha}/{tail}"


async def share(
    ws: LocalWorkspace, top: str, base: str, repo: str, remote: str, paths: list[str],
    http: Callable[..., int],
) -> str:  # fmt: skip
    picked = await pick(ws, top, ws.cwd, paths)
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(4)
    sha = await commit(ws, top, picked, name)
    ref = SHARE_REFS + name
    try:
        await push(ws, top, f"{ref}:{ref}")
    except (ShareError, TimeoutError, OSError) as exc:
        await g.git(ws, "update-ref", "-d", ref, cwd=top)
        raise ShareError(await unpushed(ws, top, remote, name, exc)) from None
    url = link(base, repo, sha, picked.named)
    status = await asyncio.to_thread(opens, http, "HEAD", url, frozenset({200}))
    if status != 200:
        failed = await withdraw(ws, top, name)
        said = f"HTTP {status}" if status else "no answer"
        then = (
            "so it was withdrawn and no link was made: try again in a minute"
            if failed is None
            else f"and it couldn't be withdrawn from GitHub ({failed}): no link was made; revoke "
            f"it with `zab share --revoke {name}`"
        )
        raise ShareError(f"the file server didn't serve the new share ({said}), {then}")
    out = [url, f"It opens only {', '.join(picked.named)}, for anyone who has the link."]
    if picked.left_out:
        out.append("Left out: " + "; ".join(f"{p} ({why})" for p, why in picked.left_out[:20]))
        if len(picked.left_out) > 20:
            out[-1] += f"; and {len(picked.left_out) - 20} more"
    out.append(
        f"It keeps working until it is revoked, after this VM ends too. Revoke it with "
        f"`zab share --revoke {name}` while this VM runs; after that, the organizers can."
    )
    return "\n".join(out)


async def unpushed(ws: LocalWorkspace, top: str, remote: str, name: str, exc: BaseException) -> str:
    """What became of a share whose push failed or was cut short, which may have landed all the
    same (the reviewer and Codex on #744: a connection can drop after GitHub took the ref):
    GitHub is asked; a share there is withdrawn; the message says which."""
    if isinstance(exc, ShareError):
        why = str(exc)
    else:
        why = f"the push to GitHub didn't finish ({type(exc).__name__})"
    try:
        landed = name in await on_github(ws, top, remote)
    except (ShareError, TimeoutError, OSError):
        landed = None  # can't tell
    if landed is False:
        return why  # nothing landed
    failed = await withdraw(ws, top, name) if landed else "GitHub can't be read to tell"
    if failed is None:
        return f"{why}; it had reached GitHub, and was withdrawn again: try again"
    return (
        f"{why}; it may be on GitHub, and couldn't be withdrawn ({failed}): try again later, "
        f"then `zab share --list`, and `zab share --revoke {name}` if it is there"
    )


async def withdraw(ws: LocalWorkspace, top: str, name: str) -> str | None:
    """A share's ref deleted on GitHub, then here: None, or why GitHub's deletion failed (the
    local ref goes either way)."""
    ref = SHARE_REFS + name
    try:
        await push(ws, top, f":{ref}")
        failed = None
    except ShareError as exc:
        failed = str(exc)
    except (TimeoutError, OSError) as exc:
        failed = f"the push didn't finish ({type(exc).__name__})"
    await g.git(ws, "update-ref", "-d", ref, cwd=top)
    return failed


async def describe(ws: LocalWorkspace, top: str, sha: str) -> tuple[str, list[str]]:
    res = await g.git(ws, "log", "-1", "--format=%cI%n%B", sha, cwd=top)
    if res.exit_code != 0:
        return "(made elsewhere)", []
    date, *body = res.stdout.splitlines()
    return date, [line[6:] for line in body if line.startswith("Path: ")]


async def listing(ws: LocalWorkspace, top: str, base: str, repo: str, remote: str) -> str:
    shares = await on_github(ws, top, remote)
    if not shares:
        return "This repository has no share links."
    lines = []
    for name, sha in sorted(shares.items()):
        date, paths = await describe(ws, top, sha)
        shown = ", ".join(paths) or "?"
        lines.append(f"{name} · {date} · {shown} · {link(base, repo, sha, paths)}")
    return "\n".join(lines)


async def revoke(
    ws: LocalWorkspace, top: str, base: str, repo: str, remote: str, what: str,
    http: Callable[..., int],
) -> str:  # fmt: skip
    shares = await on_github(ws, top, remote)
    found = _SHA.search(what.lower())
    names = [n for n, sha in shares.items() if (found and sha == found.group(1)) or n == what]
    if not names:
        raise ShareError(
            "no share link of this repository has that link or name (`zab share --list` shows "
            "them; an answer's own links aren't share links and can't be revoked)",
            2,
        )
    for name in names:
        if (failed := await withdraw(ws, top, name)) is not None:
            raise ShareError(f"{name} wasn't revoked: {failed}")
    sha, where = shares[names[0]], quote(repo, safe="")
    # the server checks it at once (200: revoked or kept; 404: no copy there); 503: try again
    recheck = f"{base}/_zab/recheck/{where}/{sha}"
    checked = await asyncio.to_thread(opens, http, "POST", recheck, frozenset({200, 404}))
    try:
        status = await asyncio.to_thread(http, "HEAD", f"{base}/{where}/{sha}/", timeout=40.0)
    except httpx.HTTPError:
        status = 0
    said = "the link no longer opens"
    # No deadline unless the server confirmed it (Codex on #744): the server drops a share only
    # at a check that reaches GitHub while the repository exists, and a repository deleted first
    # revokes nothing.
    later = (
        "it stops at the server's next check that reaches GitHub, while this repository still "
        "exists, so don't delete the repository before the link answers Not found; to have it "
        "removed at once, ask the organizers"
    )
    if status != 404 and checked not in (200, 404):  # 503: GitHub didn't say
        said = (
            "its ref is deleted on GitHub, but the file server couldn't check it with GitHub "
            f"just now: {later}"
        )
    elif status != 404:
        said = (
            f"its ref is deleted on GitHub, but the file server still served it just now: {later}"
        )
    return (
        f"Revoked {', '.join(names)}: {said}. A copy someone has already opened, or that a "
        "browser kept, can't be recalled."
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zab share",
        description="A link to some files or folders of this repository alone, for someone the "
        "participant shares them with (issue #710). Never share an answer's own link for that.",
    )
    p.add_argument("paths", nargs="*", metavar="PATH", help="files or folders to share")
    p.add_argument("--list", action="store_true", help="this repository's share links")
    p.add_argument("--revoke", metavar="LINK|NAME", help="revoke a share link")
    p.add_argument("--config", help="zab's config file (default: $ZAB_CONFIG or ~/.config/zab)")
    return p


async def run(args: argparse.Namespace, base: str, owner: str, http: Callable[..., int]) -> str:
    cwd = os.getcwd()
    ws = LocalWorkspace(cwd, cwd)
    res = await g.git(ws, "rev-parse", "--show-toplevel")
    if res.exit_code != 0 or not res.stdout.strip():
        raise ShareError("this isn't a git repository: run it in the topic's checkout", 2)
    top = res.stdout.strip()
    ws = LocalWorkspace(top, cwd)
    origin = await g._github_origin(ws)
    if origin is None or origin[0].casefold() != owner.casefold():
        raise ShareError(
            f"this repository's origin isn't a GitHub repository of {owner}: the file server "
            "serves only those",
            2,
        )
    repo, remote = origin[1].removesuffix(".git"), origin[2]
    if args.list:
        return await listing(ws, top, base, repo, remote)
    if args.revoke:
        return await revoke(ws, top, base, repo, remote, args.revoke, http)
    return await share(ws, top, base, repo, remote, args.paths, http)


def main(argv: list[str], *, http: Callable[..., int] = http_status) -> int:
    p = parser()
    args = p.parse_args(argv)
    if sum((bool(args.paths), args.list, args.revoke is not None)) != 1:
        p.error("give paths to share, or --list, or --revoke LINK|NAME")
    what = args.revoke
    if what is not None and not (_SHA.search(what.lower()) or _NAME.fullmatch(what)):
        p.error("--revoke takes a share's link or its name")
    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"zab share: cannot read zab's config: {exc}", file=sys.stderr)
        return 2
    base = config.render.refserve_url.rstrip("/")
    if not base:
        print("zab share: share links are off: no file server is configured", file=sys.stderr)
        return 2
    try:
        print(asyncio.run(run(args, base, config.render.refserve_owner, http)))
    except ShareError as exc:
        print(f"zab share: {exc}", file=sys.stderr)
        return exc.code
    except (TimeoutError, OSError) as exc:
        print(f"zab share: {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return 1
    return 0
