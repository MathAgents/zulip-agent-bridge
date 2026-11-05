#!/usr/bin/env python3
"""The VM half of `final_sync.py` (README.md §7.1): run as `agent`, read from stdin.

`final_sync.py` runs `sudo -u agent -H /usr/bin/python3 - [--dry-run]` on each VM with this
file on stdin. Only the standard library, and only argument lists (CONTRIBUTING.md, rule 3).

For the repository in `[bridge] default_project` (the participant's checkout, `~/work/<repo>`),
and for each of its worktrees (one per topic, `.zab/worktrees/<topic>-<hex>`, branch
`zab/<topic>-<hex>`):

1. If no turn is running there (zab's state database), commit what is not committed yet:
   `git add -A`, which respects `.gitignore`, minus `.lake` and `__pycache__` directories, minus
   any file over 50 MB and anything that looks like a credential (both listed, never committed),
   with `--no-verify`, as `MathAgents final sync` (not the participant).
2. Push the branch to `origin` (the deploy key), never forced: a rejected push is reported,
   unless origin's branch already contains ours. Then push every other local branch the same
   way, so that nothing exists only on the VM; a failed commit doesn't stop the push.
3. Write `TOPICS.md` on main (origin's default branch), one row per Zulip topic: its agents,
   its branch, the last commit and the branch's `HANDOVER.md` when there is one, then push main
   the same way.

It prints one JSON object (the last line of stdout) for `final_sync.py`. `--dry-run` commits,
writes and pushes nothing: it reports what it would commit and runs `git push --dry-run`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any

DRY = "--dry-run" in sys.argv[1:]
#: 50 MB; the environment variable exists for the tests only
MAX_BYTES = int(os.environ.get("ZAB_FINAL_SYNC_MAX_BYTES") or 50 * 1024 * 1024)
#: Never committed, whatever `.gitignore` says: build trees that are large and rebuildable.
EXCLUDE = [
    ":(exclude,glob)**/.lake/**",
    ":(exclude,glob)**/.lake",
    ":(exclude,glob)**/__pycache__/**",
]
#: Never committed either, and listed: anything named like a key or a credential, including
#: the deploy key's own name (`provision_repos.py` writes `~/.ssh/zab_<slug>`; review on #206).
SECRET = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|[^/]*\.pem|[^/]*\.key|id_(rsa|ecdsa|ed25519)(\.pub)?|"
    r"zab_[a-z0-9-]+(\.pub)?|[^/]*zuliprc|auth\.json|\.credentials\.json|\.netrc|"
    r"\.git-credentials|\.pypirc)$"
)
#: ... and any small file that holds a private key, whatever its name.
PRIVATE_KEY = b"PRIVATE KEY-----"
KEY_FILE_MAX = 64 * 1024
TRAILER = "Agent: zab final_sync"
#: Who the snapshot and index commits are by: this script, never the participant, whose git
#: identity is on their own commits (Drew, Sat Oct 10; #214). The same address as the
#: template's first commit (`provision_repos.COMMITTER`); it belongs to no GitHub account.
IDENTITY = ("MathAgents final sync", "mathagents@users.noreply.github.com")
GITHUB = re.compile(
    r"(?:git@github\.com(?:-[^:]+)?:|https://github\.com/|ssh://git@github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$"
)
COMMIT_MSG = (
    "End-of-workshop snapshot: work not yet committed in this topic\n\n"
    "Committed by the organizers' final sync (zab final_sync.py) before the VM was stopped,\n"
    "so that nothing exists only on the VM. Large files (over 50 MB), .lake build trees\n"
    f"and anything that looks like a credential were left out.\n\n{TRAILER}\n"
)


def git(
    cwd: str, *args: str, timeout: float = 300, as_sync: bool = False
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    if as_sync:  # the organizers' script, not the participant (Drew, Sat Oct 10; #214)
        name, email = IDENTITY
        env.update(
            GIT_AUTHOR_NAME=name,
            GIT_AUTHOR_EMAIL=email,
            GIT_COMMITTER_NAME=name,
            GIT_COMMITTER_EMAIL=email,
        )
    try:
        return subprocess.run(  # noqa: S603 - argument list
            ["git", "-C", cwd, *args],  # noqa: S607 - git from the PATH, as zab runs it
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout:g} s")


def first(text: str, limit: int = 160) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[0] if lines else "")[:limit]


def home() -> Path:
    return Path(os.path.expanduser("~"))


def project_root() -> Path:
    """`[bridge] default_project` from zab's config, else the only checkout under ~/work."""
    config = home() / ".config" / "zab" / "config.toml"
    try:
        with open(config, "rb") as f:
            path = tomllib.load(f).get("bridge", {}).get("default_project")
        if path:
            return Path(os.path.expanduser(str(path)))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    checkouts = [p for p in (home() / "work").glob("*") if (p / ".git").exists()]
    if len(checkouts) == 1:
        return checkouts[0]
    raise RuntimeError("no default_project in ~/.config/zab/config.toml")


def zab_state() -> tuple[list[dict[str, Any]], set[str], str]:
    """zab's sessions, the repo roots with a running turn, and the Zulip site (read-only)."""
    path = home() / ".local" / "state" / "zab" / "zab.db"
    if not path.exists():
        return [], set(), ""
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        sessions = [
            dict(r)
            for r in db.execute(
                "SELECT id, bot, stream_id, topic, state, repo_root, branch, card_message_id, "
                "updated_at FROM sessions ORDER BY created_at"
            )
        ]
        running_ids = {
            r[0] for r in db.execute("SELECT session_id FROM turns WHERE state='running'")
        }
        site = ""
        for (value,) in db.execute("SELECT value FROM kv WHERE key LIKE 'site:%'"):
            site = str(json.loads(value) or "")
            break
    finally:
        db.close()
    running = {
        os.path.realpath(s["repo_root"])
        for s in sessions
        if s["id"] in running_ids and s["repo_root"]
    }
    return sessions, running, site.rstrip("/")


def default_branch(repo: Path) -> str:
    """origin's default branch (`origin/HEAD`, set by the clone), else `main`, which
    `provision_repos.py` pushes; never whatever branch the checkout happens to be on."""
    res = git(str(repo), "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    name = res.stdout.strip().removeprefix("origin/") if res.returncode == 0 else ""
    return name or "main"


def local_branches(repo: Path) -> list[str]:
    res = git(str(repo), "for-each-ref", "--format=%(refname)", "refs/heads")
    if res.returncode:
        raise RuntimeError(f"git for-each-ref: {first(res.stderr)}")
    return [ref.removeprefix("refs/heads/") for ref in res.stdout.splitlines() if ref]


def worktrees(repo: Path) -> list[dict[str, str]]:
    """`git worktree list --porcelain`: the main checkout first, then one per topic."""
    res = git(str(repo), "worktree", "list", "--porcelain")
    if res.returncode:
        raise RuntimeError(f"git worktree list: {first(res.stderr)}")
    out: list[dict[str, str]] = []
    for block in res.stdout.strip().split("\n\n"):
        entry: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            entry[key] = value
        if "worktree" in entry and "prunable" not in entry:
            branch = entry.get("branch", "")
            out.append(
                {
                    "path": entry["worktree"],
                    "branch": branch.removeprefix("refs/heads/") if branch else "",
                }
            )
    return out


def changed_paths(path: str) -> list[str]:
    """Paths `git status` reports (untracked files listed one by one; ignored ones never)."""
    res = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if res.returncode:
        raise RuntimeError(f"git status: {first(res.stderr)}")
    paths, parts, i = [], res.stdout.split("\0"), 0
    while i < len(parts):
        item = parts[i]
        if len(item) > 3:
            paths.append(item[3:])
            if item[0] in "RC":  # a rename's or copy's source follows
                i += 1
        i += 1
    return [p for p in paths if "/.lake/" not in f"/{p}" and "/__pycache__/" not in f"/{p}"]


def excluded(path: str, rel: str) -> str | None:
    """Why `rel` must never be committed: `build`, `secret`, `big`, or None."""
    if "/.lake/" in f"/{rel}/" or "/__pycache__/" in f"/{rel}/":
        return "build"
    if SECRET.search(rel):
        return "secret"
    full = os.path.join(path, rel)
    if not os.path.isfile(full) or os.path.islink(full):  # git commits a link, not its target
        return None
    size = os.path.getsize(full)
    if size > MAX_BYTES:
        return "big"
    if size <= KEY_FILE_MAX:
        try:
            with open(full, "rb") as f:
                if PRIVATE_KEY in f.read(KEY_FILE_MAX):
                    return "secret"
        except OSError:
            pass
    return None


def describe(path: str, rel: str, why: str) -> str:
    if why == "big":
        return f"{rel} ({os.path.getsize(os.path.join(path, rel)) // (1024 * 1024)} MB)"
    return rel


def commit_worktree(path: str) -> dict[str, Any]:
    """Step 1 for one worktree: what is (or would be) committed, and what is left out.

    The exclusions are checked twice: on what `git status` reports (for the report, and the
    dry run) and on the index after `git add`, so that something an agent had already staged
    (a `.lake` file, a big file: codex review on #206) is unstaged too. The big and secret
    paths found first are kept out of `git add` itself, so that a multi-gigabyte file is never
    hashed into the object store (codex round 2 on #206)."""
    big, secret, keep, skip = [], [], [], []
    for p in changed_paths(path):
        why = excluded(path, p)
        if why == "big":
            big.append(describe(path, p, why))
        elif why == "secret":
            secret.append(p)
        elif why is None:
            keep.append(p)
        if why in ("big", "secret"):
            skip.append(f":(exclude,literal){p}")
    result: dict[str, Any] = {"files": len(keep), "big": big, "secret": secret}
    if not keep:  # nothing to commit (whatever may already be staged stays uncommitted)
        result["state"] = "clean" if not (big or secret) else "nothing committable"
        return result
    if DRY:
        result["state"] = "would commit"
        return result
    res = git(path, "add", "-A", "--", ".", *EXCLUDE, *skip)
    if res.returncode:
        raise RuntimeError(f"git add: {first(res.stderr)}")
    staged = git(path, "diff", "--cached", "--name-only", "-z", "--diff-filter=d")
    if staged.returncode:
        raise RuntimeError(f"git diff --cached: {first(staged.stderr)}")
    left_out = []
    for rel in filter(None, staged.stdout.split("\0")):
        why = excluded(path, rel)
        if why:
            left_out.append(rel)
            shown = describe(path, rel, why)
            if why == "big" and shown not in big:
                big.append(shown)
            elif why == "secret" and rel not in secret:
                secret.append(rel)
    if left_out:
        res = git(path, "reset", "-q", "--", *(f":(literal){p}" for p in left_out))
        if res.returncode:
            raise RuntimeError(f"git reset of excluded paths: {first(res.stderr)}")
    if git(path, "diff", "--cached", "--quiet").returncode == 0:
        result["state"] = "clean" if not (big or secret) else "nothing committable"
        return result
    res = git(path, "commit", "-q", "--no-verify", "-m", COMMIT_MSG, as_sync=True)
    if res.returncode:
        raise RuntimeError(f"git commit: {first(res.stderr or res.stdout)}")
    result["state"] = "committed"
    return result


def push(path: str, branch: str) -> str:
    """Step 2: `ok`, `up to date`, `behind origin …`, `rejected: …` or `error: …` (never forced)."""
    args = ["push", "--porcelain"] + (["--dry-run"] if DRY else [])
    res = git(path, *args, "origin", f"refs/heads/{branch}:refs/heads/{branch}", timeout=180)
    flags = [ln.split("\t", 1)[0] for ln in res.stdout.splitlines() if "\t" in ln]
    if res.returncode == 0:
        if flags and all(f == "=" for f in flags):
            return "up to date"
        return "would push" if DRY else "ok"
    if "!" in flags or "rejected" in res.stderr:
        if behind_origin(path, branch):
            return "behind origin (nothing to push)"
        return "rejected: " + push_reason(res)
    return "error: " + push_reason(res)


def behind_origin(path: str, branch: str) -> bool:
    """Whether origin's branch already contains ours, e.g. main after a PR merged on GitHub:
    a rejection then loses nothing (review on #206)."""
    if git(path, "fetch", "-q", "origin", f"refs/heads/{branch}", timeout=180).returncode:
        return False
    return (
        git(path, "merge-base", "--is-ancestor", f"refs/heads/{branch}", "FETCH_HEAD").returncode
        == 0
    )


def push_reason(res: subprocess.CompletedProcess[str]) -> str:
    """GitHub's own reason (a file over 100 MB: `… exceeds GitHub's file size limit`, GH001) or
    the porcelain summary (`[rejected] (non-fast-forward)`), not `failed to push some refs`."""
    remote = [ln for ln in res.stderr.splitlines() if ln.startswith("remote:")]
    for needle in ("exceeds", "GH0"):
        if hit := next((ln for ln in remote if needle in ln), None):
            return first(hit)
    for ln in res.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3 and parts[0] == "!":
            return first(parts[2])
    return first(res.stderr) or f"exit {res.returncode}"


def github_repo(repo: Path) -> tuple[str, str] | None:
    res = git(str(repo), "config", "--get", "remote.origin.url")  # as written, not rewritten
    m = GITHUB.search(res.stdout.strip()) if res.returncode == 0 else None
    return (m[1], m[2]) if m else None


def cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def blob(repo: Path, ref: str, path: str = "HANDOVER.md") -> str:
    res = git(str(repo), "rev-parse", "-q", "--verify", f"{ref}:{path}")
    return res.stdout.strip() if res.returncode == 0 else ""


def created_at(repo: Path, branch: str) -> str:
    """The commit `branch` was created at (its reflog's oldest entry), or "" without a reflog."""
    res = git(str(repo), "rev-list", "--walk-reflogs", f"refs/heads/{branch}")
    created = res.stdout.split()[-1:] if res.returncode == 0 else []
    return created[0] if created else ""


def since_created(repo: Path, branch: str, main: str) -> str:
    """The commits made on `branch` since it was created (its reflog's oldest entry, so that a
    branch already merged into main still shows its work), else those not on main."""
    ref = f"refs/heads/{branch}"
    if branch == main:
        return ref
    if created := created_at(repo, branch):
        return f"{created}..{ref}"
    has_main = git(str(repo), "rev-parse", "-q", "--verify", f"refs/heads/{main}").returncode == 0
    return f"refs/heads/{main}..{ref}" if has_main else ref


def last_commit(repo: Path, branch: str, main: str) -> str:
    """The branch's last commit of its own (not main's, not `TOPICS.md`'s), preferring the
    agents' own over the final sync's snapshot, which is noted instead (review on #206)."""
    fmt = "--format=%H %cs · %s"
    rng = since_created(repo, branch, main)
    paths = ["--", ".", ":(exclude)TOPICS.md"]
    tip = git(str(repo), "log", "-1", fmt, rng, *paths).stdout.strip()
    if not tip:
        return "—"
    own = git(
        str(repo), "log", "-1", fmt, "--invert-grep", f"--grep=^{TRAILER}$", rng, *paths
    ).stdout.strip()
    if not own:  # nothing but the snapshot
        return cell(first(tip.split(" ", 1)[1], 80))
    text = cell(first(own.split(" ", 1)[1], 80))
    return text if own.split(" ", 1)[0] == tip.split(" ", 1)[0] else f"{text} (+ snapshot)"


def topics_md(
    repo: Path, sessions: list[dict[str, Any]], site: str, pushed: dict[str, str], main: str
) -> str:
    """`TOPICS.md`: one row per Zulip topic and branch, with its agents, last commit and
    handover. A topic whose card was deleted gets a new session in a new worktree, so it can
    have several branches: the live session's comes first, and its card is linked."""
    gh = github_repo(repo)
    topics: dict[tuple[int, str], dict[str, Any]] = {}
    # sessions come ordered by created_at; detached ones last, keeping that order otherwise
    for s in sorted(sessions, key=lambda s: s.get("state") == "detached"):
        key = (int(s["stream_id"]), str(s["topic"]))
        t = topics.setdefault(key, {"card": None, "branches": {}})
        t["card"] = t["card"] or s.get("card_message_id")
        bots = t["branches"].setdefault(s.get("branch") or main, [])  # shared mode: main
        if (bot := str(s["bot"]).capitalize()) not in bots:
            bots.append(bot)
    # every branch inherits the template's skeleton HANDOVER.md (workshop/template/), or whatever
    # main held when it was created: link only one an agent wrote, i.e. one that changed on
    # the branch since it was created, so a topic merged into main keeps its link and a topic
    # created after that merge doesn't take it over (review on #206). Without a reflog (and for
    # main itself): neither main's first version (the skeleton) nor main's current one.
    added = git(
        str(repo),
        "log",
        "--format=%H",
        "--diff-filter=A",
        f"refs/heads/{main}",
        "--",
        "HANDOVER.md",
    ).stdout.split()
    template = blob(repo, added[-1]) if added else ""
    on_main = blob(repo, f"refs/heads/{main}")
    lines = [
        "# Workshop topics",
        "",
        "Written at the end of the workshop (Oct 10, 2026). Each Zulip topic worked on its own",
        "branch of this repository. Where an agent wrote one, `HANDOVER.md` on that branch says",
        "where the work stands and how to pick it up. To bring a topic's work into main, ask an",
        "agent to merge its branch.",
        "",
        "| Topic | Agents | Branch | Last commit | Handover |",
        "|---|---|---|---|---|",
    ]
    for (stream_id, topic), t in sorted(topics.items(), key=lambda kv: kv[0][1].lower()):
        name = cell(topic)
        if site and t["card"]:
            name = f"[{name}]({site}/#narrow/channel/{stream_id}/near/{t['card']})"
        for branch, bots in t["branches"].items():
            quoted = urllib.parse.quote(branch, safe="/")
            last = last_commit(repo, branch, main)
            handover = "—"
            mine = blob(repo, f"refs/heads/{branch}")
            base = created_at(repo, branch) if branch != main else ""
            if base:
                wrote = bool(mine) and mine != blob(repo, base)
            else:
                wrote = bool(mine) and mine != template and (branch == main or mine != on_main)
            if wrote:
                handover = (
                    f"[HANDOVER.md](https://github.com/{gh[0]}/{gh[1]}/blob/{quoted}/HANDOVER.md)"
                    if gh
                    else "`HANDOVER.md`"
                )
            state = pushed.get(branch, "")
            shown = f"`{cell(branch)}`"
            if gh:
                shown = f"[{shown}](https://github.com/{gh[0]}/{gh[1]}/tree/{quoted})"
            if state.startswith(("rejected", "error")):
                shown += " (not on GitHub: the push failed)"
            elif not state:  # every local branch is pushed, so this one is gone from the VM
                shown += " (not found on the VM)"
            lines.append(f"| {name} | {', '.join(bots)} | {shown} | {last} | {handover} |")
    return "\n".join(lines) + "\n"


def write_index(repo: Path, text: str, main: str, busy: bool) -> str:
    """Step 3: commit `TOPICS.md` on main (fast-forwarded first) and push it."""
    if busy:
        return "skipped: a turn is running in the main checkout"
    if DRY:
        return "would write"
    current = git(str(repo), "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if current != main:
        return f"skipped: the checkout is on {current or 'a detached HEAD'}, not {main}"
    fetch = git(str(repo), "fetch", "-q", "origin", main, timeout=180)
    if fetch.returncode == 0:
        ff = git(str(repo), "merge", "-q", "--ff-only", f"origin/{main}")
        if ff.returncode:
            return f"skipped: {main} has diverged from origin/{main}"
    target = repo / "TOPICS.md"
    if target.exists() and target.read_text(encoding="utf-8") == text:
        state = "unchanged"
    else:
        target.write_text(text, encoding="utf-8")
        git(str(repo), "add", "--", "TOPICS.md")
        res = git(
            str(repo),
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "Add TOPICS.md: the workshop's topics, their branches and handover notes\n\n"
            "Agent: zab final_sync\n",
            "--",
            "TOPICS.md",
            as_sync=True,
        )
        if res.returncode:
            return "error: " + first(res.stderr or res.stdout)
        state = "written"
    pushed = push(str(repo), main)
    fine = ("ok", "up to date", "behind origin (nothing to push)")
    return state if pushed in fine else f"{state}, push {pushed}"


def main() -> dict[str, Any]:
    report: dict[str, Any] = {"dry_run": DRY, "worktrees": [], "errors": []}
    try:
        repo = project_root()
        report["repo"] = str(repo)
        sessions, running, site = zab_state()
        report["sessions"] = len(sessions)
        trees = worktrees(repo)
    except Exception as exc:  # reported, never raised: the laptop tabulates every VM
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        return report
    pushed: dict[str, str] = {}
    main_branch = default_branch(repo)
    for tree in trees:
        entry: dict[str, Any] = {"path": tree["path"], "branch": tree["branch"]}
        try:
            if os.path.realpath(tree["path"]) in running:
                entry["state"] = "turn running: not committed"
            else:
                entry.update(commit_worktree(tree["path"]))
        except Exception as exc:  # the branch's earlier commits are pushed all the same
            entry["state"] = f"error: {exc}"
        try:
            if tree["branch"]:
                entry["push"] = pushed[tree["branch"]] = push(tree["path"], tree["branch"])
            else:
                entry["push"] = "detached HEAD: not pushed"
        except Exception as exc:
            entry["push"] = f"error: {exc}"
        report["worktrees"].append(entry)
    # branches no worktree has checked out (an agent's `git checkout -b idea` and back, or a
    # session's branch after its worktree moved on): nothing may exist only on the VM
    report["branches"] = []
    try:
        for branch in local_branches(repo):
            if branch not in pushed:
                pushed[branch] = push(str(repo), branch)
                report["branches"].append({"branch": branch, "push": pushed[branch]})
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    if trees and sessions:
        try:
            text = topics_md(repo, sessions, site, pushed, main_branch)
            busy = os.path.realpath(trees[0]["path"]) in running
            report["index"] = write_index(repo, text, main_branch, busy)
        except Exception as exc:
            report["index"] = f"error: {type(exc).__name__}: {exc}"
    else:
        report["index"] = "skipped: no sessions"
    return report


if __name__ == "__main__":
    started = time.monotonic()
    out = main()
    out["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(out))
