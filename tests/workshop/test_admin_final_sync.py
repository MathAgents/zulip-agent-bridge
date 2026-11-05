"""`workshop/admin/final_sync.py` and `final_sync_remote.py` (workshop/admin/README.md §7.1).

The remote half runs against real git repositories in a temporary HOME: a bare repository as
`origin` (reached through an `insteadOf` rewrite of the deploy-key alias URL), the
participant's checkout with two topic worktrees, and zab's state database made by `Store`.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import admin_common as ac
import final_sync as fs
import final_sync_remote as remote
import idle_stop as idle
import pytest

from zab.core.store import Store

REMOTE = pathlib.Path(fs.REMOTE_SCRIPT)
ALIAS = "git@github.com-alice:MathAgents/alice.git"


def git(cwd, *args):
    cmd = ["git", "-C", str(cwd), *args]
    res = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def session(sid, bot, topic, repo_root, branch, card, mode="worktree"):
    now = time.time()
    return dict(
        id=sid,
        bot=bot,
        backend=bot,
        native_id=f"native-{sid}",
        card_message_id=card,
        stream_id=642445,
        topic=topic,
        workspace=str(repo_root),
        workspace_mode=mode,
        repo_root=str(repo_root),
        branch=branch,
        settings_json="{}",
        state="idle",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def vm(tmp_path):
    """HOME with ~/work/alice (main + two topic worktrees), zab's config and state database."""
    home = tmp_path / "home"
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    repo = home / "work" / "alice"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Alice Smith")
    git(repo, "config", "user.email", "alice@example.org")
    git(repo, "config", f"url.{bare}.insteadOf", ALIAS)
    git(repo, "remote", "add", "origin", ALIAS)
    (repo / "README.md").write_text("# alice\n")
    (repo / "HANDOVER.md").write_text("# Handover\n\n(the template's skeleton)\n")
    (repo / ".gitignore").write_text("*.log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "template")
    git(repo, "push", "-q", "origin", "main")
    (repo / ".git" / "info" / "exclude").write_text(".zab/\n")
    trees = {}
    for name in ("zeta-1a2b3c", "busy-4d5e6f"):
        path = repo / ".zab" / "worktrees" / name
        git(repo, "worktree", "add", "-q", "-b", f"zab/{name}", str(path), "HEAD")
        trees[name] = path
    # topic "Zeta": a handover committed earlier, then uncommitted work of every kind
    zeta = trees["zeta-1a2b3c"]
    (zeta / "HANDOVER.md").write_text("where things stand\n")
    git(zeta, "add", "HANDOVER.md")
    git(zeta, "commit", "-q", "-m", "Handover")
    (zeta / "notes.md").write_text("results\n")
    (zeta / "proof" / ".lake" / "build").mkdir(parents=True)
    (zeta / "proof" / ".lake" / "build" / "Big.olean").write_text("x" * 100)
    (zeta / "proof" / "Main.lean").parent.mkdir(exist_ok=True)
    (zeta / "proof" / "Main.lean").write_text("theorem t : True := trivial\n")
    (zeta / ".env").write_text("TOKEN=secret\n")
    (zeta / "data.bin").write_bytes(b"0" * 2048)  # over the test's 1 KiB limit
    (zeta / "run.log").write_text("ignored by .gitignore\n")
    # topic "Busy": a commit not pushed yet, and an edit while a turn is running
    busy = trees["busy-4d5e6f"]
    (busy / "a.py").write_text("print(1)\n")
    git(busy, "add", "a.py")
    git(busy, "commit", "-q", "-m", "a.py")
    (busy / "a.py").write_text("print(2)\n")
    # zab's config and state
    config = home / ".config" / "zab"
    config.mkdir(parents=True)
    (config / "config.toml").write_text('[bridge]\ndefault_project = "~/work/alice"\n')
    store = Store(home / ".local" / "state" / "zab")
    store.insert("sessions", **session("s1", "claude", "Zeta | zeros", zeta, "zab/zeta-1a2b3c", 11))
    store.insert("sessions", **session("s2", "codex", "Zeta | zeros", zeta, "zab/zeta-1a2b3c", 12))
    store.insert("sessions", **session("s3", "claude", "Busy", busy, "zab/busy-4d5e6f", 13))
    store.insert("sessions", **session("s4", "codex", "Start here", repo, None, 14, "shared"))
    store.insert(
        "turns",
        session_id="s3",
        native_turn_id="t1",
        trigger_msg_ids="[]",
        state="running",
        started_at=time.time(),
    )
    store.set("site:claude", "https://mathagents.zulipchat.com")
    store.db.close()
    return {"home": home, "bare": bare, "repo": repo, **trees}


def run_remote(home, *args):
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "ZAB_FINAL_SYNC_MAX_BYTES": "1024",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    res = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(REMOTE), *args], capture_output=True, text=True, env=env
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def by_branch(report):
    return {t["branch"]: t for t in report["worktrees"]}


def remote_files(bare, ref):
    return set(git(bare, "ls-tree", "-r", "--name-only", ref).splitlines())


def test_dry_run_reports_and_changes_nothing(vm):
    before = git(vm["bare"], "for-each-ref", "--format=%(refname)")
    report = run_remote(vm["home"], "--dry-run")
    trees = by_branch(report)
    assert report["dry_run"] and report["sessions"] == 4 and not report["errors"]
    zeta = trees["zab/zeta-1a2b3c"]
    assert zeta["state"] == "would commit" and zeta["files"] == 2  # notes.md, proof/Main.lean
    assert zeta["secret"] == [".env"] and zeta["big"] == ["data.bin (0 MB)"]
    assert zeta["push"] == "would push"
    assert trees["zab/busy-4d5e6f"]["state"] == "turn running: not committed"
    assert report["index"] == "would write"
    assert git(vm["bare"], "for-each-ref", "--format=%(refname)") == before
    assert "notes.md" in git(vm["zeta-1a2b3c"], "status", "--porcelain")


def test_commits_pushes_and_indexes_then_a_rerun_is_a_no_op(vm):
    report = run_remote(vm["home"])
    trees = by_branch(report)
    zeta = trees["zab/zeta-1a2b3c"]
    assert zeta["state"] == "committed" and zeta["push"] == "ok"
    files = remote_files(vm["bare"], "zab/zeta-1a2b3c")
    assert {"notes.md", "proof/Main.lean", "HANDOVER.md"} <= files
    # never: .lake, the credential-like file, the big file, what .gitignore ignores
    assert not any(".lake" in f for f in files)
    assert not files & {".env", "data.bin", "run.log"}
    # by the organizers' script, not the participant (#214), whose own commit stays theirs
    message = git(vm["bare"], "log", "-1", "--format=%an <%ae>|%cn|%B", "zab/zeta-1a2b3c")
    assert message.startswith(
        "MathAgents final sync <mathagents@users.noreply.github.com>|MathAgents final sync|"
        "End-of-workshop snapshot"
    )
    assert "Agent: zab final_sync" in message
    assert git(vm["bare"], "log", "-1", "--format=%an", "zab/zeta-1a2b3c~1") == "Alice Smith"
    # a running turn's worktree: its commits pushed, its uncommitted edit left alone
    busy = trees["zab/busy-4d5e6f"]
    assert busy["state"] == "turn running: not committed" and busy["push"] == "ok"
    assert "a.py" in remote_files(vm["bare"], "zab/busy-4d5e6f")
    assert "print(1)" in git(vm["bare"], "show", "zab/busy-4d5e6f:a.py")
    assert "M a.py" in git(vm["busy-4d5e6f"], "status", "--porcelain")  # still uncommitted
    # TOPICS.md on main, pushed
    assert report["index"] == "written"
    index = git(vm["bare"], "show", "main:TOPICS.md")
    assert "| Topic | Agents | Branch | Last commit | Handover |" in index
    assert git(vm["bare"], "log", "-1", "--format=%an|%cn", "main") == (
        "MathAgents final sync|MathAgents final sync"
    )
    zeta_row = next(line for line in index.splitlines() if "Zeta" in line)
    assert (
        "[Zeta \\| zeros](https://mathagents.zulipchat.com/#narrow/channel/642445/near/11)"
        in zeta_row
    )
    assert "| Claude, Codex |" in zeta_row
    assert "https://github.com/MathAgents/alice/tree/zab/zeta-1a2b3c" in zeta_row
    assert "https://github.com/MathAgents/alice/blob/zab/zeta-1a2b3c/HANDOVER.md" in zeta_row
    assert "· Handover (+ snapshot) |" in zeta_row  # the agents' own last commit
    # the template's skeleton HANDOVER.md, inherited unchanged, is not linked
    busy_row = next(line for line in index.splitlines() if "Busy" in line)
    assert "· a.py |" in busy_row and busy_row.endswith("| — |")
    start_row = next(line for line in index.splitlines() if "Start here" in line)
    assert "tree/main" in start_row and start_row.endswith("| — |")
    # a second run finds nothing to do
    again = run_remote(vm["home"])
    again_trees = by_branch(again)
    assert again_trees["zab/zeta-1a2b3c"]["state"] == "nothing committable"
    assert again_trees["zab/zeta-1a2b3c"]["push"] == "up to date"
    assert again["index"] == "unchanged"


def test_files_an_agent_had_already_staged_are_still_left_out(vm):
    """codex review on #206: `git add`'s exclusions don't touch what is already in the index."""
    zeta = vm["zeta-1a2b3c"]
    git(zeta, "add", "-f", "proof/.lake/build/Big.olean", "data.bin", ".env")
    report = run_remote(vm["home"])
    entry = by_branch(report)["zab/zeta-1a2b3c"]
    assert entry["state"] == "committed"
    assert entry["big"] == ["data.bin (0 MB)"] and entry["secret"] == [".env"]
    files = remote_files(vm["bare"], "zab/zeta-1a2b3c")
    assert "notes.md" in files
    assert not any(".lake" in f for f in files) and not files & {".env", "data.bin"}


def test_a_rejected_push_is_reported_never_forced(vm):
    # someone pushed to the topic branch from elsewhere: ours is no longer a fast-forward
    other = vm["home"] / "other"
    git(vm["home"], "clone", "-q", str(vm["bare"]), str(other))
    git(other, "config", "user.name", "Bob")
    git(other, "config", "user.email", "bob@example.org")
    git(other, "checkout", "-q", "-b", "zab/zeta-1a2b3c")
    (other / "x.md").write_text("elsewhere\n")
    git(other, "add", "x.md")
    git(other, "commit", "-q", "-m", "elsewhere")
    git(other, "push", "-q", "origin", "zab/zeta-1a2b3c")
    report = run_remote(vm["home"])
    zeta = by_branch(report)["zab/zeta-1a2b3c"]
    assert zeta["state"] == "committed" and zeta["push"].startswith("rejected")
    assert git(vm["bare"], "log", "-1", "--format=%s", "zab/zeta-1a2b3c") == "elsewhere"
    index = git(vm["bare"], "show", "main:TOPICS.md")
    assert "(not on GitHub: the push failed)" in index


def test_a_failed_commit_still_pushes_the_branch(vm):
    """Review on #206: a commit step that fails (a stale index.lock, a nested repository with no
    commit) must not keep the branch's earlier commits on the VM."""
    (vm["repo"] / ".git" / "worktrees" / "zeta-1a2b3c" / "index.lock").write_text("")
    report = run_remote(vm["home"])
    zeta = by_branch(report)["zab/zeta-1a2b3c"]
    assert zeta["state"].startswith("error: git add") and zeta["push"] == "ok"
    assert "HANDOVER.md" in remote_files(vm["bare"], "zab/zeta-1a2b3c")
    assert "zab/zeta-1a2b3c: error: git add" in fs.SyncResult("alice", report=report).row()[-1]


def push_from_elsewhere(vm, branch):
    other = vm["home"] / "other"
    git(vm["home"], "clone", "-q", "-b", branch, str(vm["bare"]), str(other))
    (other / "merged.md").write_text("merged on GitHub\n")
    git(other, "add", "merged.md")
    git(other, "-c", "user.name=Bob", "-c", "user.email=b@example.org", "commit", "-q", "-m", "PR")
    git(other, "push", "-q", "origin", branch)


def test_a_branch_behind_origin_is_not_a_rejection(vm):
    """Review on #206: origin's main moved on (a PR merged on GitHub); ours loses nothing."""
    push_from_elsewhere(vm, "main")
    report = run_remote(vm["home"])
    assert by_branch(report)["main"]["push"] == "behind origin (nothing to push)"
    assert report["index"] == "written"  # fast-forwarded, then pushed
    assert "merged.md" in remote_files(vm["bare"], "main")
    assert "main" not in fs.SyncResult("alice", report=report).row()[-1]


def test_a_rejection_says_why():
    rejected = subprocess.CompletedProcess(
        [],
        1,
        "To github.com:o/r.git\n"
        "!\trefs/heads/b:refs/heads/b\t[rejected] (non-fast-forward)\nDone\n",
        "error: failed to push some refs to 'github.com:o/r.git'\n",
    )
    assert remote.push_reason(rejected) == "[rejected] (non-fast-forward)"
    too_big = subprocess.CompletedProcess(
        [],
        1,
        "!\trefs/heads/b:refs/heads/b\t[remote rejected] (pre-receive hook declined)\n",
        "remote: error: Trace: 8f6b\nremote: error: See https://gh.io/lfs for more information.\n"
        "remote: error: File out/big.bin is 120.00 MB; this exceeds GitHub's file size limit "
        "of 100.00 MB\nremote: error: GH001: Large files detected.\n",
    )
    assert remote.push_reason(too_big).startswith("remote: error: File out/big.bin is 120.00 MB")


def test_the_index_goes_on_main_only(vm):
    """Review on #206: not on whatever branch the checkout happens to be on."""
    git(vm["repo"], "checkout", "-q", "-b", "experiment")
    report = run_remote(vm["home"])
    assert report["index"] == "skipped: the checkout is on experiment, not main"
    assert "TOPICS.md" not in remote_files(vm["bare"], "main")
    assert "TOPICS.md" not in remote_files(vm["bare"], "experiment")


def test_branches_no_worktree_has_checked_out_are_pushed_too(vm):
    """Review on #206: an agent's own branch, and a session's branch its worktree moved off."""
    zeta = vm["zeta-1a2b3c"]
    git(zeta, "checkout", "-q", "-b", "zeta-idea")
    (zeta / "idea.md").write_text("idea\n")
    git(zeta, "add", "idea.md")
    git(zeta, "commit", "-q", "-m", "idea")
    git(zeta, "branch", "-q", "zeta-backup", "zeta-idea")
    report = run_remote(vm["home"])
    # the worktree is on zeta-idea now; zab/zeta-1a2b3c (the session's branch) and
    # zeta-backup are on no worktree, and pushed all the same
    assert by_branch(report)["zeta-idea"]["push"] == "ok"
    assert {b["branch"]: b["push"] for b in report["branches"]} == {
        "zab/zeta-1a2b3c": "ok",
        "zeta-backup": "ok",
    }
    assert "idea.md" in remote_files(vm["bare"], "zeta-backup")
    assert "HANDOVER.md" in remote_files(vm["bare"], "zab/zeta-1a2b3c")
    index = git(vm["bare"], "show", "main:TOPICS.md")
    zeta_row = next(line for line in index.splitlines() if "Zeta" in line)
    assert "tree/zab/zeta-1a2b3c" in zeta_row and "(not" not in zeta_row
    row = fs.SyncResult("alice", report=report).row()
    assert row[4] == "4"  # main is up to date; zab/busy, zeta-idea, zab/zeta, zeta-backup


def test_a_session_branch_missing_from_the_vm_is_marked(vm):
    git(vm["repo"], "worktree", "remove", "--force", str(vm["busy-4d5e6f"]))
    git(vm["repo"], "branch", "-q", "-D", "zab/busy-4d5e6f")
    run_remote(vm["home"])
    index = git(vm["bare"], "show", "main:TOPICS.md")
    busy_row = next(line for line in index.splitlines() if "Busy" in line)
    assert "(not found on the VM)" in busy_row


def test_big_and_secret_files_never_reach_the_object_store(vm):
    """Codex round 2 on #206: kept out of `git add` itself, not added and then unstaged, so a
    multi-gigabyte dataset is never hashed into .git."""
    zeta = vm["zeta-1a2b3c"]
    blobs = [git(zeta, "hash-object", name) for name in ("data.bin", ".env")]
    report = run_remote(vm["home"])
    assert by_branch(report)["zab/zeta-1a2b3c"]["state"] == "committed"
    for blob in blobs:
        res = subprocess.run(["git", "-C", str(zeta), "cat-file", "-e", blob])  # noqa: S603, S607
        assert res.returncode != 0


def test_a_detached_worktree_is_a_problem(vm):
    """Codex round 2 on #206: its commits are on no branch, so they exist only on the VM."""
    git(vm["zeta-1a2b3c"], "checkout", "-q", "--detach")
    report = run_remote(vm["home"])
    zeta = next(t for t in report["worktrees"] if t["path"] == str(vm["zeta-1a2b3c"]))
    assert zeta["push"] == "detached HEAD: not pushed"
    assert "zeta-1a2b3c: push detached HEAD" in fs.SyncResult("alice", report=report).row()[-1]


@pytest.mark.parametrize(
    ("index", "problem"),
    [
        ("written", False),
        ("unchanged", False),
        ("would write", False),
        ("skipped: no sessions", False),
        ("skipped: a turn is running in the main checkout", False),
        ("written, push error: timed out after 180 s", True),
        ("error: OSError: disk full", True),
        ("skipped: the checkout is on experiment, not main", True),
        ("skipped: main has diverged from origin/main", True),
    ],
)
def test_a_failed_index_is_a_problem(index, problem):
    """Codex round 2 on #206: no TOPICS.md on GitHub fails the run."""
    row = fs.SyncResult("alice", report={"worktrees": [], "index": index, "errors": []}).row()
    assert (f"TOPICS.md: {index}" in row[-1]) == problem


def test_a_topic_with_a_new_session_lists_both_branches_the_live_one_first(vm):
    """Review on #206: a deleted card detaches the topic's sessions, and the next message
    starts a new session in a new worktree; the index shows both, the live one first."""
    new = vm["repo"] / ".zab" / "worktrees" / "zeta-9f8e7d"
    git(vm["repo"], "worktree", "add", "-q", "-b", "zab/zeta-9f8e7d", str(new), "main")
    (new / "fresh.md").write_text("started again\n")
    store = Store(vm["home"] / ".local" / "state" / "zab")
    store.update("sessions", {"id": "s1"}, state="detached")
    store.update("sessions", {"id": "s2"}, state="detached")
    store.insert("sessions", **session("s5", "claude", "Zeta | zeros", new, "zab/zeta-9f8e7d", 21))
    store.db.close()
    run_remote(vm["home"])
    index = git(vm["bare"], "show", "main:TOPICS.md")
    rows = [line for line in index.splitlines() if "Zeta" in line]
    assert len(rows) == 2 and all("near/21)" in row for row in rows)
    assert "tree/zab/zeta-9f8e7d" in rows[0] and "| Claude |" in rows[0]
    assert "· End-of-workshop snapshot" in rows[0]  # nothing but the snapshot on it
    assert "tree/zab/zeta-1a2b3c" in rows[1] and "| Claude, Codex |" in rows[1]


def test_a_branch_already_merged_into_main_still_shows_its_last_commit(vm):
    git(vm["repo"], "merge", "-q", "--no-edit", "zab/busy-4d5e6f")
    run_remote(vm["home"])
    index = git(vm["bare"], "show", "main:TOPICS.md")
    busy_row = next(line for line in index.splitlines() if "Busy" in line)
    assert "· a.py |" in busy_row


def test_a_handover_is_linked_on_the_branch_that_wrote_it(vm):
    """Review on #206: Zeta's handover, merged into main, stays linked on Zeta's row, and a topic
    created after that merge, which inherited it unchanged, doesn't link it once main moves on."""
    repo = vm["repo"]
    git(repo, "merge", "-q", "--no-edit", "zab/zeta-1a2b3c")
    late = repo / ".zab" / "worktrees" / "late-111111"
    git(repo, "worktree", "add", "-q", "-b", "zab/late-111111", str(late), "main")
    (late / "x.md").write_text("x\n")
    (repo / "HANDOVER.md").write_text("main moved on\n")
    git(repo, "commit", "-q", "-am", "main's handover")
    store = Store(vm["home"] / ".local" / "state" / "zab")
    store.insert("sessions", **session("s9", "claude", "Late", late, "zab/late-111111", 30))
    store.db.close()
    run_remote(vm["home"])
    index = git(vm["bare"], "show", "main:TOPICS.md")
    row = {t: next(ln for ln in index.splitlines() if t in ln) for t in ("Zeta", "Late")}
    assert "blob/zab/zeta-1a2b3c/HANDOVER.md" in row["Zeta"]
    assert row["Late"].endswith("| — |")


@pytest.mark.parametrize(
    ("name", "content", "why"),
    [
        (".git-credentials", "https://u:t@github.com\n", "secret"),
        ("sub/.pypirc", "[pypi]\n", "secret"),
        ("keys/zab_alice-smith", "key\n", "secret"),
        ("keys/zab_alice-smith.pub", "ssh-ed25519 AAAA\n", "secret"),
        ("id_ed25519.pub", "ssh-ed25519 AAAA\n", "secret"),
        ("copied.txt", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl\n", "secret"),
        ("zab_notes.md", "notes\n", None),
        ("id_card.md", "notes\n", None),
        ("notes.md", "a key idea\n", None),
    ],
)
def test_credential_names_and_private_keys_are_left_out(tmp_path, name, content, why):
    """Review on #206: the deploy key's own name, .git-credentials, .pypirc, and any small
    file holding a private key."""
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(content)
    assert remote.excluded(str(tmp_path), name) == why


def test_without_zab_config_or_state_it_reports_instead_of_failing(tmp_path):
    report = run_remote(tmp_path)
    assert report["errors"] and "default_project" in report["errors"][0]


# ------------------------------------------------------------------------- the laptop half


class FakeRunner:
    def __init__(self, stdout="", stderr="", code=0):
        self.calls = []
        self.result = ac.RunResult(code, stdout, stderr)

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.calls.append((list(argv), stdin))
        return self.result


ROW = {"slug": "alice", "ip": "203.0.113.5", "repo_status": "ok"}


def test_sync_vm_runs_the_fixed_command_with_the_script_on_stdin(tmp_path):
    report = {
        "worktrees": [
            {"branch": "main", "state": "clean", "push": "up to date"},
            {"branch": "zab/a", "state": "committed", "push": "ok", "big": ["x (60 MB)"]},
            {"branch": "zab/b", "state": "turn running: not committed", "push": "rejected: x"},
        ],
        "index": "written",
        "errors": [],
    }
    runner = FakeRunner(stdout="noise\n" + json.dumps(report) + "\n")
    res = fs.sync_vm(ROW, runner, dry_run=True, script="SCRIPT", known_hosts=str(tmp_path / "kh"))
    argv, stdin = runner.calls[0]
    assert argv[-1] == fs.REMOTE_CMD_DRY and argv[-3] == "ubuntu@203.0.113.5" and stdin == "SCRIPT"
    assert "alice" not in " ".join(argv)  # nothing from the roster but the pinned IP
    row = res.row()
    assert row[:7] == ["alice", "203.0.113.5", "3", "1", "1", "1", "written"]
    assert "zab/b: push rejected: x" in row[7] and "1 with a turn running" in row[7]


@pytest.mark.parametrize("status", ["", "owner check: ssh: connect timed out", "failed"])
def test_strict_fails_a_skipped_row_that_has_a_vm(status):
    """`--strict`, the admin bot's mode (the review of #367): a `repo_status` other than `ok`
    is no proof that the VM holds no work, so such a row with an `ip` is a failure, not a skip;
    without an `ip` it has no VM, and stays skipped."""
    runner = FakeRunner()
    res = fs.sync_vm(dict(ROW, repo_status=status), runner, dry_run=False, script="S", strict=True)
    assert not runner.calls and not res.skipped
    assert res.row()[-1].startswith("has a VM but ") and res.row()[-1].endswith(": not synced")
    res = fs.sync_vm(
        dict(ROW, repo_status=status, ip=""), runner, dry_run=False, script="S", strict=True
    )
    assert res.skipped and not runner.calls


def test_main_strict_exits_non_zero_for_a_skipped_row_with_a_vm(tmp_path, monkeypatch, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,first_name,last_name,email,ip,repo_status\nbob,B,J,b@example.org,203.0.113.6,\n"
    )
    monkeypatch.setattr(fs, "subprocess_runner", FakeRunner())
    args = ["--roster", str(roster), "--out-dir", str(tmp_path / "out"), "--known-hosts", "kh"]
    assert fs.main(args) == 0  # by hand: skipped, as before
    assert fs.main([*args, "--strict"]) == 1
    assert "1 VM(s) with problems" in capsys.readouterr().out


def test_sync_vm_skips_unprovisioned_rows_and_reports_ssh_failures():
    runner = FakeRunner()
    res = fs.sync_vm(dict(ROW, repo_status=""), runner, dry_run=False, script="S")
    assert not runner.calls and res.row()[-1] == "not provisioned: nothing to sync"
    failing = FakeRunner(
        stderr="ssh: connect to host 203.0.113.5 port 22: No route to host", code=255
    )
    res = fs.sync_vm(ROW, failing, dry_run=False, script="S")
    assert res.row()[-1].startswith("no report: ssh: connect")


def test_main_fails_when_a_vm_cannot_be_reached_but_not_for_unprovisioned_rows(
    tmp_path, monkeypatch, capsys
):
    """codex review on #206: an unreachable VM's work may not be on GitHub, so it fails the run."""
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,first_name,last_name,email,ip,repo_status\n"
        "alice,A,S,a@example.org,203.0.113.5,ok\n"
        "bob,B,J,b@example.org,203.0.113.6,\n"
    )
    unreachable = FakeRunner(stderr="ssh: connect to host 203.0.113.5: No route", code=255)
    monkeypatch.setattr(fs, "subprocess_runner", unreachable)
    args = ["--roster", str(roster), "--out-dir", str(tmp_path / "out"), "--known-hosts", "kh"]
    assert fs.main(args) == 1
    out = capsys.readouterr().out
    assert "1 VM(s) with problems" in out and "1 row(s) not provisioned (skipped)" in out
    healthy = FakeRunner(stdout=json.dumps({"worktrees": [], "index": "unchanged", "errors": []}))
    monkeypatch.setattr(fs, "subprocess_runner", healthy)
    assert fs.main(args) == 0
    assert "no problems" in capsys.readouterr().out


def test_the_remote_commands_are_fixed_literals():
    assert fs.REMOTE_CMD == "cd / && sudo -u agent -H /usr/bin/python3 -"
    assert fs.REMOTE_CMD_DRY == fs.REMOTE_CMD + " --dry-run"
    assert fs.parse_report('x\n{"a": 1}\n') == {"a": 1} and fs.parse_report("{oops") is None


# ------------------------------------------------------- the idle-stop hold around a sync (#370)


class IdleVM:
    """`vm` behind SSH with idle stop on, its agents uninstalled (#370). The hold's commands
    run for real in bash, on a hold file under tmp_path for /run and without `sudo`. Idle
    stop's timer fires once as the sync starts (`idle_stop.main`), with nothing else to keep
    the VM up: up 2 hours, no activity, no login, load 0.05. A tick that powers the VM off
    cuts the sync's connection; otherwise the sync runs on `vm`."""

    def __init__(self, tmp_path, home, *, hold_fails=False):
        self.tmp, self.home, self.hold_fails = tmp_path, home, hold_fails
        (tmp_path / "run").mkdir()
        self.hold = tmp_path / "run" / "zab-idle-stop.hold"
        self.proc = tmp_path / "proc"
        self.proc.mkdir()
        (self.proc / "uptime").write_text("7200.00 50000.00\n")
        (self.proc / "loadavg").write_text("0.10 0.05 0.01 1/300 4242\n")
        Store(tmp_path / "idle-db").db.close()  # zab's schema, nothing in it: idle for hours
        self.events: list[str] = []
        self.off = False

    def tick(self):
        """One run of `zab-idle-stop.service`, with `--minutes 10`, the shortest."""

        def run(argv, **kwargs):
            assert argv in (["who"], idle.POWEROFF), argv  # no zab rows: nothing else to ask
            self.off = self.off or argv == idle.POWEROFF
            return subprocess.CompletedProcess(argv, 0, "", "")

        log = self.tmp / "idle.log"
        warmed = self.tmp / "zab-warm.done"  # its first boot's warm-up long done (#715)
        warmed.touch()
        idle.main(
            ["--minutes", "10", "--db", str(self.tmp / "idle-db" / "zab.db")],
            run=run,
            proc=str(self.proc),
            log=str(log),
            last=str(self.tmp / "run" / "last"),
            login=str(self.tmp / "run" / "login"),
            hold=str(self.hold),
            euid=1000,
            warm=str(warmed),
        )
        self.events.append("tick: " + log.read_text().splitlines()[-1].split(" ", 1)[1])

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        assert argv[0] == "ssh" and argv[-3] == "ubuntu@203.0.113.5"
        remote = argv[-1]
        if remote in (fs.IDLE_HOLD_ON_CMD, fs.IDLE_HOLD_OFF_CMD):
            self.events.append("hold on" if remote == fs.IDLE_HOLD_ON_CMD else "hold off")
            if self.hold_fails:
                return ac.RunResult(1, "", "sudo: a password is required\n")
            return local_shell(remote, self.hold)
        assert remote == fs.REMOTE_CMD, remote
        self.tick()  # the timer fires mid-sync
        if self.off:
            return ac.RunResult(255, "", "Connection to 203.0.113.5 closed by remote host.\n")
        self.events.append("sync")
        env = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "ZAB_FINAL_SYNC_MAX_BYTES": "1024",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        res = subprocess.run(
            [sys.executable, "-"], input=stdin, capture_output=True, text=True, env=env
        )
        return ac.RunResult(res.returncode, res.stdout, res.stderr)


def local_shell(remote, hold):
    """A hold command as the VM's shell runs it, on `hold` for /run's file, without sudo."""
    local = remote.replace("sudo ", "").replace("/run/zab-idle-stop.hold", str(hold))
    res = subprocess.run(["bash", "-c", local], capture_output=True, text=True)  # noqa: S603, S607
    return ac.RunResult(res.returncode, res.stdout, res.stderr)


def test_the_sync_holds_idle_stop_off_while_it_runs(vm, tmp_path):
    """#370: a VM whose idle-stop timer fires mid-sync stays up, the sync completes, and the
    hold is released after it: the next tick powers the VM off."""
    machine = IdleVM(tmp_path, vm["home"])
    res = fs.sync_vm(ROW, machine, dry_run=False, script=REMOTE.read_text(), known_hosts="kh")
    assert machine.events[0] == "hold on" and not machine.off
    assert machine.events[1].startswith("tick: stay up: held by a rollout until "), machine.events
    assert machine.events[2:] == ["sync", "hold off"]
    zeta = by_branch(res.report)["zab/zeta-1a2b3c"]
    assert (zeta["state"], zeta["push"], res.report["index"]) == ("committed", "ok", "written")
    assert "notes.md" in remote_files(vm["bare"], "zab/zeta-1a2b3c")
    assert res.hold == "released" and not machine.hold.exists()
    machine.tick()  # without the hold, the same tick powers the VM off
    assert machine.off and machine.events[-1].startswith("tick: power off: idle for 10 min")


def test_without_the_hold_the_same_tick_cuts_the_sync(vm, tmp_path):
    """The control: a hold that can't be taken (`sudo` refused) doesn't stop the sync from
    being tried, and this VM then powers off under it; nothing is pushed."""
    machine = IdleVM(tmp_path, vm["home"], hold_fails=True)
    res = fs.sync_vm(ROW, machine, dry_run=False, script=REMOTE.read_text(), known_hosts="kh")
    assert machine.events[0] == "hold on" and len(machine.events) == 2 and machine.off
    assert machine.events[1].startswith("tick: power off: idle for 10 min")
    assert res.problems == ["no report: Connection to 203.0.113.5 closed by remote host."]
    assert res.hold == "not taken: sudo: a password is required"
    assert git(vm["bare"], "for-each-ref", "--format=%(refname)") == "refs/heads/main"


class Scripted:
    """A VM whose answer is given per remote command: the hold's, the sync's, the release's."""

    def __init__(self, sync, *, on=None, off=None):
        ok = ac.RunResult(0, "", "")
        self.answers = {
            fs.IDLE_HOLD_ON_CMD: on or ok,
            fs.REMOTE_CMD: sync,
            fs.REMOTE_CMD_DRY: sync,
            fs.IDLE_HOLD_OFF_CMD: off or ok,
        }
        self.remotes = []

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        self.remotes.append(argv[-1])
        answer = self.answers[argv[-1]]
        if isinstance(answer, BaseException):
            raise answer
        return answer


REPORT = json.dumps({"worktrees": [], "index": "unchanged", "errors": []})
HOLD = [fs.IDLE_HOLD_ON_CMD, fs.REMOTE_CMD, fs.IDLE_HOLD_OFF_CMD]


@pytest.mark.parametrize(
    "sync",
    [
        ac.RunResult(0, REPORT, ""),
        ac.RunResult(1, "", "sudo: unknown user agent"),  # ended on the VM, failed
        ac.RunResult(0, '{"errors": ["RuntimeError: no default_project"]}', ""),
        ac.RunResult(255, REPORT, "Connection to 203.0.113.5 closed."),  # the report came
    ],
)
def test_the_hold_is_released_once_the_sync_has_ended_on_the_vm(sync):
    runner = Scripted(sync)
    res = fs.sync_vm(ROW, runner, dry_run=False, script="S")
    assert runner.remotes == HOLD and res.hold == "released"


@pytest.mark.parametrize(
    "sync",
    [
        ac.RunResult(-1, "", "timed out after 1800 s: ssh"),
        ac.RunResult(255, "", "Connection to 203.0.113.5 closed by remote host."),
        ac.RunResult(-9, "", ""),  # the client SIGKILLed (review of #462): the sync runs on
    ],
)
def test_a_cut_sync_leaves_the_hold_to_lapse_since_it_may_still_run(sync):
    runner = Scripted(sync)
    res = fs.sync_vm(ROW, runner, dry_run=False, script="S")
    assert runner.remotes == HOLD[:2] and res.problems == [f"no report: {fs.cause(sync)}"]
    assert res.hold == "left to lapse: the sync's connection was cut"
    runner = Scripted(KeyboardInterrupt())  # an interrupt propagates, the hold left on
    with pytest.raises(KeyboardInterrupt):
        fs.sync_vm(ROW, runner, dry_run=False, script="S")
    assert runner.remotes == HOLD[:2]


@pytest.mark.parametrize(
    "on",
    [
        ac.RunResult(255, "", "ssh: connect to host 203.0.113.5 port 22: Connection timed out"),
        # hung after `tee`, perhaps: the hold may be on (review of #462), and a sync over the
        # same connection would likely hang too, for up to 30 minutes of the apply's 30
        ac.RunResult(-1, "", "timed out after 60 s: ssh"),
    ],
)
def test_a_vm_whose_hold_connection_fails_or_hangs_is_not_synced(on):
    runner = Scripted(ac.RunResult(0, REPORT, ""), on=on)
    res = fs.sync_vm(ROW, runner, dry_run=False, script="S")
    assert runner.remotes == HOLD[:1] and res.problems == [f"no report: {on.stderr}"]
    assert res.hold == f"not confirmed: {on.stderr}"  # it lapses within the hour if it is on


def test_a_failed_release_is_reported_and_a_dry_run_takes_no_hold():
    runner = Scripted(ac.RunResult(0, REPORT, ""), off=ac.RunResult(1, "", "rm: read-only"))
    res = fs.sync_vm(ROW, runner, dry_run=False, script="S")
    assert runner.remotes == HOLD and not res.problems  # the sync's outcome: not a problem
    assert res.hold == "not released (it lapses within the hour): rm: read-only"
    runner = Scripted(ac.RunResult(0, REPORT, ""))
    fs.sync_vm(ROW, runner, dry_run=True, script="S")  # a dry run writes nothing: no hold
    assert runner.remotes == [fs.REMOTE_CMD_DRY]


def test_the_hold_is_push_configs_and_idle_stop_reads_it_as_an_hour_ahead(tmp_path):
    import push_config as pc

    assert (fs.IDLE_HOLD_ON_CMD, fs.IDLE_HOLD_OFF_CMD) == (
        pc.IDLE_HOLD_ON_CMD,
        pc.IDLE_HOLD_OFF_CMD,
    )
    hold = tmp_path / "hold"
    before = time.time()
    assert local_shell(fs.IDLE_HOLD_ON_CMD, hold).ok
    assert int(before) + 3600 <= idle.read_hold(str(hold)) <= time.time() + 3600
    assert local_shell(fs.IDLE_HOLD_OFF_CMD, hold).ok and idle.read_hold(str(hold)) is None


def test_the_report_says_what_became_of_each_hold(tmp_path, monkeypatch):
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,first_name,last_name,email,ip,repo_status\nalice,A,S,a@example.org,203.0.113.5,ok\n"
    )
    monkeypatch.setattr(fs, "subprocess_runner", Scripted(ac.RunResult(0, REPORT, "")))
    out = tmp_path / "out"
    assert fs.main(["--roster", str(roster), "--out-dir", str(out), "--known-hosts", "kh"]) == 0
    (path,) = out.glob("final_sync-*.json")
    assert json.loads(path.read_text())["alice"]["idle_hold"] == "released"


# ------------------------------------------------------------- a repository that's gone (#714)


class FakeGitHub:
    """`repo` answers by name: a dict (found, with its owner), None (a 404) or an exception, or a
    list of those, one per call; `private_repos` the listing, or an exception."""

    def __init__(self, repos=None, listing=("bob",)):
        self.repos, self.listing, self.calls = dict(repos or {}), listing, []

    def repo(self, org, name):
        self.calls.append(("repo", org, name))
        answer = self.repos.get(name, {"owner": {"login": org}})
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def private_repos(self, org):
        self.calls.append(("list", org))
        if isinstance(self.listing, BaseException):
            raise self.listing
        return list(self.listing)


def refused(status):
    import provision_repos as pr

    return pr.GitHubError("GET", "/repos/MathAgents/alice", status, {"message": "x"})


@pytest.mark.parametrize(
    "repos,listing,gone",
    [
        ({"alice": None}, ("bob",), True),  # deleted
        ({"alice": {"owner": {"login": "alice-own"}}}, ("bob",), True),  # transferred, readable
        # Codex on #738: GitHub names its new owner, so no other repository need be read
        ({"alice": {"owner": {"login": "alice-own"}}}, (), True),
        ({"alice": {"owner": {"login": "alice-own"}}}, refused(401), True),
        ({}, ("bob",), False),  # there
        ({"alice": {"owner": {"login": "MathAgents"}, "name": "a2"}}, ("bob",), False),  # renamed
        ({"alice": [None, {"owner": {"login": "MathAgents"}}]}, ("bob",), False),  # a blip
        ({"alice": None}, ("bob", "alice"), False),  # its listing shows it
        ({"alice": None}, (), False),  # the token reads nothing private
        ({"alice": None, "bob": None}, ("bob",), False),  # nor the other one
        ({"alice": None, "bob": refused(503)}, ("bob",), False),
        ({"alice": None}, refused(401), False),  # an expired or revoked token
        ({"alice": refused(401)}, ("bob",), False),
        ({"alice": refused(502)}, ("bob",), False),  # an outage
        # a 200 that names no owner proves nothing (reviewer on #738)
        ({"alice": {"name": "alice"}}, ("bob",), False),
        ({"alice": {"owner": {}}}, ("bob",), False),
        ({"alice": {"owner": {"login": ""}}}, (), False),
        # ... and on the confirming second look, after a 404 (the reviewer's re-check)
        ({"alice": [None, {"name": "alice"}]}, ("bob",), False),
        ({"alice": [None, {"owner": {"login": ""}}]}, ("bob",), False),
        ({"alice": [None, {"owner": {"login": "alice-own"}}]}, ("bob",), True),  # moved since
        ({"alice": OSError("unreachable")}, ("bob",), False),
    ],
)
def test_a_repository_is_gone_only_when_github_says_so_and_reads_another(repos, listing, gone):
    import provision_repos as pr

    gh = FakeGitHub(repos, listing)
    assert pr.repo_gone(gh, "MathAgents", "alice") is gone
    if gone and isinstance(repos["alice"], dict):  # at another owner's: that one read alone
        assert gh.calls == [("repo", "MathAgents", "alice")]
    elif gone:  # not found, the listing, another read, not found again
        assert [c[0] for c in gh.calls] == ["repo", "list", "repo", "repo"]
        assert [c[2] for c in gh.calls if c[0] == "repo"] == ["alice", "bob", "alice"]


def test_a_gone_repository_is_reported_and_its_vm_not_reached():
    runner = FakeRunner()
    row = dict(ROW, repo="MathAgents/alice")
    res = fs.sync_vm(row, runner, dry_run=False, script="S", gh=FakeGitHub({"alice": None}))
    assert not runner.calls and res.gone and not res.skipped
    assert res.row()[-1] == (
        "MathAgents/alice no longer exists here: the participant deleted or transferred it"
    )
    # without a token, or with a `repo` that isn't one, the row is synced as before
    for gh, value in ((None, "MathAgents/alice"), (FakeGitHub({"alice": None}), "x/../y")):
        runner = FakeRunner(stdout=json.dumps({"worktrees": [], "index": "unchanged"}))
        res = fs.sync_vm(dict(ROW, repo=value), runner, dry_run=False, script="S", gh=gh)
        assert runner.calls and not res.gone


def test_main_exits_gone_when_every_failure_is_a_gone_repository(tmp_path, monkeypatch, capsys):
    roster = tmp_path / "roster.csv"
    roster.write_text(
        "slug,first_name,last_name,email,ip,repo,repo_status\n"
        "alice,A,S,a@example.org,203.0.113.5,MathAgents/alice,ok\n"
        "bob,B,J,b@example.org,203.0.113.6,MathAgents/bob,ok\n"
    )
    healthy = FakeRunner(stdout=json.dumps({"worktrees": [], "index": "unchanged", "errors": []}))
    monkeypatch.setattr(fs, "subprocess_runner", healthy)
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    made = []

    def client(token):
        made.append(token)
        return FakeGitHub({"alice": None})

    monkeypatch.setattr(fs, "GitHub", client)
    args = ["--roster", str(roster), "--out-dir", str(tmp_path / "out"), "--known-hosts", "kh"]
    assert fs.main([*args, "--strict"]) == fs.GONE == 4 and made == ["synthetic-token"]
    out = capsys.readouterr().out
    assert "MathAgents/alice no longer exists here: the participant deleted or transferred" in out
    assert "synthetic-token" not in out
    hosts = {arg for argv, _ in healthy.calls for arg in argv if arg.startswith("ubuntu@")}
    assert hosts == {"ubuntu@203.0.113.6"}  # bob's VM alone was reached
    unreachable = FakeRunner(stderr="ssh: connect to host 203.0.113.6: No route", code=255)
    monkeypatch.setattr(fs, "subprocess_runner", unreachable)
    assert fs.main([*args, "--strict"]) == 1  # bob's failure is another one
