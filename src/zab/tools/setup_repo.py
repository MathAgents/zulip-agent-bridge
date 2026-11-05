"""`zab setup-repo OWNER/REPO [--path DIR] [--yes]`: the deploy-key flow (SPEC §13.1).

For machines without GitHub credentials (the workshop VMs): one ed25519
key per repository, an SSH host alias `github.com-<repo>` (which `workspace/git.py` recognizes,
SPEC §10.4), a clone, and a push test. No token ever touches the machine. The six steps are
functions that take a `Runner`, so tests inject a fake and a temporary HOME.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable
from typing import TextIO

from zab.tools.common import Runner, first_line, short_host, subprocess_runner

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")

SSH_CONFIG_BLOCK = """
# zab setup-repo: deploy key for {owner}/{repo} (SPEC §13.1)
Host github.com-{repo}
    HostName github.com
    User git
    IdentityFile {key}
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
"""


def parse_repo(spec: str) -> tuple[str, str]:
    """`OWNER/REPO` → (owner, repo); both restricted to GitHub's name characters."""
    parts = spec.strip().split("/")
    if len(parts) != 2 or not all(_NAME_RE.match(p) for p in parts) or parts[1].endswith(".git"):
        raise ValueError(f"expected OWNER/REPO, got {spec!r}")
    return parts[0], parts[1]


def key_path(home: str, repo: str) -> str:
    return os.path.join(home, ".ssh", f"zab_{repo}")


def record_path(home: str) -> str:
    """Where setup-repo records the checkout it configured, for `zab preflight`."""
    return os.path.join(home, ".local", "state", "zab", "repo")


def record_checkout(home: str, path: str) -> None:
    """Remember `path` so that `zab preflight` tests the push from the right checkout."""
    target = record_path(home)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(os.path.abspath(path) + "\n")
    os.replace(tmp, target)


def remote_url(owner: str, repo: str) -> str:
    """The clone URL through the alias host, e.g. `git@github.com-r:o/r.git`."""
    return f"git@github.com-{repo}:{owner}/{repo}.git"


def ensure_key(run: Runner, home: str, repo: str, host: str) -> str:
    """Step 1: generate `~/.ssh/zab_<repo>` (ed25519, no passphrase) unless it exists.

    Returns the public key text.
    """
    ssh_dir = os.path.join(home, ".ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    key = key_path(home, repo)
    if not os.path.exists(key):
        result = run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"zab-{host}-{repo}", "-f", key],
            timeout_s=60,
        )
        if not result.ok:
            raise RuntimeError(
                f"ssh-keygen failed: {first_line(result.stderr) or result.exit_code}"
            )
    with open(key + ".pub", encoding="utf-8") as f:
        return f.read().strip()


def ensure_ssh_config(home: str, owner: str, repo: str) -> bool:
    """Step 2: add the `Host github.com-<repo>` block to `~/.ssh/config` if absent.

    Returns True when the block was added. Creates the file with mode 0600.
    """
    path = os.path.join(home, ".ssh", "config")
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    if re.search(rf"(?m)^\s*Host\s+github\.com-{re.escape(repo)}\s*$", existing):
        return False
    block = SSH_CONFIG_BLOCK.format(owner=owner, repo=repo, key=key_path(home, repo))
    with open(path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(block)
    os.chmod(path, 0o600)
    return True


def instructions(pubkey: str, owner: str, repo: str) -> str:
    """Step 3: what the participant has to paste where."""
    return (
        f"\nAdd this deploy key to https://github.com/{owner}/{repo}/settings/keys/new\n"
        f"Title: zab (anything works). Tick ☑ **Allow write access**.\n\n{pubkey}\n"
    )


def clone_or_set_remote(run: Runner, url: str, path: str) -> str:
    """Step 5: clone into `path`, or point an existing checkout's `origin` at the alias URL.

    Returns "cloned" or "remote set".
    """
    if os.path.isdir(os.path.join(path, ".git")):
        has_origin = run(["git", "-C", path, "remote", "get-url", "origin"], timeout_s=30).ok
        verb = "set-url" if has_origin else "add"
        result = run(["git", "-C", path, "remote", verb, "origin", url], timeout_s=30)
        if not result.ok:
            raise RuntimeError(f"git remote {verb} failed: {first_line(result.stderr)}")
        return "remote set"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    result = run(["git", "clone", "--", url, path], timeout_s=600, env=_git_env())
    if not result.ok:
        raise RuntimeError(f"git clone failed: {first_line(result.stderr)}")
    return "cloned"


def verify_write_access(run: Runner, path: str, host: str) -> tuple[bool, str]:
    """Step 6: push HEAD to `zab-preflight-<host>` on origin, then delete that branch.

    Returns (ok, detail). The detail on failure names the fix.
    """
    branch = f"zab-preflight-{host}"
    push = run(
        ["git", "-C", path, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"],
        timeout_s=120,
        env=_git_env(),
    )
    if not push.ok:
        why = first_line(push.stderr) or f"exit {push.exit_code}"
        return False, (f"push rejected ({why}); re-add the deploy key with “Allow write access”")
    delete = run(
        ["git", "-C", path, "push", "--quiet", "origin", "--delete", branch],
        timeout_s=120,
        env=_git_env(),
    )
    if not delete.ok:
        return True, f"pushed {branch}, but could not delete it: {first_line(delete.stderr)}"
    return True, f"pushed and deleted {branch}"


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def setup_repo(
    spec: str,
    *,
    path: str | None = None,
    yes: bool = False,
    run: Runner = subprocess_runner,
    home: str | None = None,
    host: str | None = None,
    out: TextIO = sys.stdout,
    wait: Callable[[], None] | None = None,
) -> int:
    """All six steps (SPEC §13.1); returns the exit code."""
    owner, repo = parse_repo(spec)
    home = home or os.path.expanduser("~")
    host = host or short_host()
    path = os.path.expanduser(path) if path else os.path.join(home, "work", repo)
    pubkey = ensure_key(run, home, repo, host)
    added = ensure_ssh_config(home, owner, repo)
    print(f"key: {key_path(home, repo)}{'' if added else ' (ssh config already set up)'}", file=out)
    print(instructions(pubkey, owner, repo), file=out)
    if not yes:
        print("Press Enter once the key is added…", file=out, flush=True)
        (wait or (lambda: sys.stdin.readline()))()
    how = clone_or_set_remote(run, remote_url(owner, repo), path)
    record_checkout(home, path)
    print(f"{how}: {path}", file=out)
    ok, detail = verify_write_access(run, path, host)
    print(("write access OK: " if ok else "write access FAILED: ") + detail, file=out)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zab setup-repo", description="Deploy-key setup for one GitHub repository."
    )
    parser.add_argument("repo", metavar="OWNER/REPO")
    parser.add_argument("--path", help="checkout directory (default ~/work/REPO)")
    parser.add_argument("--yes", action="store_true", help="do not wait for Enter")
    args = parser.parse_args(argv)
    try:
        return setup_repo(args.repo, path=args.path, yes=args.yes)
    except (ValueError, RuntimeError) as exc:
        print(f"zab setup-repo: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
