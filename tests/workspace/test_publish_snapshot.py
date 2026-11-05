"""Turn snapshots for refserve (SPEC §10.7, rc6, issue #36): what goes in, what stays out, and
that the branch, the index and the working tree are untouched."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

import pytest

from zab.workspace import git as g
from zab.workspace.local import LocalWorkspace
from zab.workspace.manager import LocalWorkspaceManager

from .conftest import make_repo, run_git


def tree(repo: Path, sha: str) -> set[str]:
    return set(run_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())


def state(repo: Path) -> tuple[str, str, str, str]:
    """HEAD, the staged entries, the porcelain status and the stash list."""
    return (
        run_git(repo, "rev-parse", "HEAD"),
        run_git(repo, "ls-files", "--stage"),
        run_git(repo, "status", "--porcelain", "--untracked-files=all"),
        run_git(repo, "stash", "list"),
    )


async def test_the_snapshot_holds_the_working_tree_minus_secrets_and_big_files(repo: Path):
    (repo / ".gitignore").write_text("build/\n")
    (repo / ".env").write_text("TOKEN=decoy\n")  # tracked by mistake: still left out
    (repo / "gone.txt").write_text("bye\n")
    (repo / "keep.lean").write_text("theorem old : True := trivial\n")
    run_git(repo, "add", ".gitignore", ".env", "gone.txt", "keep.lean")
    run_git(repo, "commit", "-q", "-m", "more")
    head = run_git(repo, "rev-parse", "HEAD")
    # the turn's work, none of it staged or committed
    (repo / "keep.lean").write_text("theorem new : True := trivial\n")
    (repo / "gone.txt").unlink()
    (repo / "report").mkdir()
    (repo / "report" / "index.html").write_text("<h1>result</h1>\n")
    (repo / "report" / "style.css").write_text("h1 { color: navy }\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_bytes(b"ignored")
    (repo / "big.dat").write_bytes(b"x" * 2048)  # over the test's 1 KiB cap
    (repo / "id_rsa").write_text("decoy key\n")  # denylisted name, untracked
    (repo / "secret-source.txt").write_text("decoy credential\n")
    os.link(repo / "secret-source.txt", repo / "innocent.txt")  # a second hard link
    os.symlink("keep.lean", repo / "alias.lean")
    (repo / "staged.txt").write_text("in the real index only\n")
    run_git(repo, "add", "staged.txt")  # the participant staged it: the snapshot has it too
    before = state(repo)
    ws = LocalWorkspace(str(repo))
    sha = await g.snapshot_commit(ws, "7/42", file_cap=1024)
    assert sha is not None and len(sha) == 40
    assert state(repo) == before  # branch, index, working tree and stash untouched
    assert run_git(repo, "rev-parse", "refs/zab/turns/7/42") == sha
    # no parent (codex on #133): a push carries no unpublished history; the base is named
    assert run_git(repo, "rev-list", "--parents", "-n", "1", sha) == sha
    assert head in run_git(repo, "log", "-1", "--format=%B", sha)
    files = tree(repo, sha)
    assert {
        "README.md",
        ".gitignore",
        "keep.lean",
        "report/index.html",
        "report/style.css",
    } <= files
    assert {"staged.txt", "alias.lean"} <= files
    assert run_git(repo, "show", f"{sha}:keep.lean") == "theorem new : True := trivial"
    for left_out in (".env", "gone.txt", "build/out.bin", "big.dat", "id_rsa", "innocent.txt"):
        assert left_out not in files, left_out
    assert "secret-source.txt" not in files  # also two links (the same inode)
    assert (
        run_git(repo, "cat-file", "-p", f"{sha}:alias.lean") == "keep.lean"
    )  # a link, as git stores it


async def test_the_total_budget_and_a_repository_without_commits(tmp_path: Path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    run_git(repo, "init", "-q")
    for i in range(5):
        (repo / f"f{i}.txt").write_bytes(b"y" * 400)
    sha = await g.snapshot_commit(LocalWorkspace(str(repo)), "1/1", file_cap=1024, total_cap=1000)
    assert sha is not None
    assert len(tree(repo, sha)) == 2  # 400 + 400 fit the 1000-byte budget, the third does not
    assert run_git(repo, "rev-list", "--parents", "-n", "1", sha) == sha  # no parent


SESSION = str(uuid.UUID("d41a70c0-ee01-40fe-8692-a0491855d7ac"))  # as core/creation.py makes


async def test_newly_staged_files_count_as_tracked(repo: Path):
    """codex P2 on #133: the temporary index starts from HEAD, so files staged in the real index
    looked untracked; one force-added past .gitignore, or over the cap, was dropped."""
    (repo / ".gitignore").write_text("*.html\n")
    run_git(repo, "add", ".gitignore")
    run_git(repo, "commit", "-q", "-m", "ignore html")
    (repo / "report.html").write_text("<h1>staged on purpose</h1>\n")
    (repo / "data.bin").write_bytes(b"z" * 4096)  # over the test's 1 KiB cap for untracked files
    (repo / "id_ed25519").write_text("decoy\n")  # staged, but denylisted: still left out
    run_git(repo, "add", "-f", "report.html", "data.bin", "id_ed25519")
    (repo / "loose.bin").write_bytes(b"z" * 4096)  # untracked and over the cap: left out
    sha = await g.snapshot_commit(LocalWorkspace(str(repo)), f"{SESSION}/8", file_cap=1024)
    files = tree(repo, sha)
    assert {"report.html", "data.bin"} <= files
    assert "id_ed25519" not in files and "loose.bin" not in files


async def test_a_changed_tracked_file_with_a_second_hard_link_leaves_the_snapshot(repo: Path):
    """codex P2 on #133: skipping `add -u` for it left HEAD's version in the snapshot."""
    (repo / "notes.txt").write_text("committed\n")
    run_git(repo, "add", "notes.txt")
    run_git(repo, "commit", "-q", "-m", "notes")
    (repo / "notes.txt").write_text("changed, and hard-linked\n")
    os.link(repo / "notes.txt", repo / "elsewhere.txt")
    sha = await g.snapshot_commit(LocalWorkspace(str(repo)), f"{SESSION}/11")
    files = tree(repo, sha)
    assert "notes.txt" not in files and "elsewhere.txt" not in files
    assert "README.md" in files


def look_alikes(top: bytes) -> None:
    """`\\xff/.env` and `\\ufffd/.env` (as bytes: not valid UTF-8, and its replacement)."""
    for directory in (b"\xff", "\ufffd".encode()):
        os.mkdir(top + b"/" + directory)
        Path(os.fsdecode(top + b"/" + directory + b"/.env")).write_text("TOKEN=decoy\n")


async def test_a_denylisted_name_that_does_not_round_trip_through_utf8_fails_closed(repo: Path):
    """codex P1 on #133 (after merge): `\\xff/.env` and `\\ufffd/.env` both decode to
    `\ufffd/.env`, so the removal pathspec matched only the second and the first, a credential,
    stayed in the pushed snapshot. Now nothing is published."""
    look_alikes(os.fsencode(repo))
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "two look-alike directories")
    assert await g.snapshot_commit(LocalWorkspace(str(repo)), f"{SESSION}/13") is None
    assert run_git(repo, "for-each-ref", "refs/zab") == ""


async def test_names_are_session_ids_and_turn_rows_and_non_repositories_give_none(tmp_path):
    """PM's blocker on #133: session ids are UUIDs, so `<uuid>/<turn>` must pass."""
    repo = make_repo(tmp_path / "r")
    ws = LocalWorkspace(str(repo))
    assert await g.snapshot_commit(ws, f"{SESSION}/17") is not None
    assert await g.snapshot_commit(ws, f"{uuid.uuid4()}/1") is not None
    for bad in ("1", "a/b", "1/2/3", "../1", "1:2", "1/2\n", "-x/1", f"{SESSION}/x", "/1"):
        assert await g.snapshot_commit(ws, bad) is None
        assert await g.publish_snapshot(ws, bad, github_owner="MathAgents") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert await g.snapshot_commit(LocalWorkspace(str(plain)), "1/2") is None


def github_checkout(
    tmp_path: Path, fake_github, origin: str = "git@github.com-alice:MathAgents/alice.git"
):
    """A checkout whose origin is the `zab setup-repo` alias URL, served by `fake_github`
    (MathAgents/alice.git), with main already there."""
    repo = make_repo(tmp_path / "alice")
    bare = fake_github("MathAgents/alice.git")
    run_git(repo, "remote", "add", "origin", origin)
    run_git(repo, "push", "-q", str(bare), "main")
    return repo, bare


async def test_the_manager_commits_and_pushes_a_uuid_named_ref_and_no_branch(
    tmp_path: Path, fake_github
):
    """Through the real LocalWorkspaceManager and a real repository (PM on #133): the ref is
    pushed under its own name, the remote's branches stay as they were."""
    repo, bare = github_checkout(tmp_path, fake_github)
    branches = run_git(bare, "for-each-ref", "refs/heads")
    manager = LocalWorkspaceManager()
    ws = LocalWorkspace(str(repo))
    (repo / "notes.md").write_text("a result\n")
    first = await manager.publish_snapshot(ws, f"{SESSION}/1", github_owner="mathagents")
    (repo / "README.md").write_text("changed\n")
    second = await manager.publish_snapshot(ws, f"{SESSION}/2", github_owner="MathAgents")
    assert first and second and first.sha != second.sha
    assert first.repo == second.repo == "alice"
    assert run_git(repo, "show", f"{first.sha}:README.md") == "hello"
    assert run_git(repo, "show", f"{second.sha}:README.md") == "changed"
    pushed = run_git(bare, "for-each-ref", "--format=%(objectname) %(refname)", "refs/zab")
    assert pushed.splitlines() == [
        f"{first.sha} refs/zab/turns/{SESSION}/1",
        f"{second.sha} refs/zab/turns/{SESSION}/2",
    ]
    assert run_git(bare, "for-each-ref", "refs/heads") == branches  # no branch pushed
    assert "notes.md" in run_git(bare, "ls-tree", "--name-only", first.sha)


async def test_a_name_is_published_once_so_retries_keep_their_links(tmp_path: Path, fake_github):
    """Codex review on #134: a completion retried after posting part of the answer must not
    move the ref (the posted links would point to a commit that is no longer a ref tip); the
    retry pushes the same ref again, which a failed first push needs."""
    repo, bare = github_checkout(tmp_path, fake_github)
    ws = LocalWorkspace(str(repo))
    first = await g.publish_snapshot(ws, f"{SESSION}/9", github_owner="MathAgents")
    (repo / "README.md").write_text("changed after the first attempt\n")
    run_git(bare, "update-ref", "-d", f"refs/zab/turns/{SESSION}/9")  # as if the push failed
    again = await g.publish_snapshot(ws, f"{SESSION}/9", github_owner="MathAgents")
    assert first and again == first
    assert run_git(bare, "rev-parse", f"refs/zab/turns/{SESSION}/9") == first.sha
    later = await g.publish_snapshot(ws, f"{SESSION}/10", github_owner="MathAgents")
    assert later and later.sha != first.sha  # the next turn snapshots anew


async def test_unpublished_history_and_its_secrets_never_reach_the_remote(
    tmp_path: Path, fake_github
):
    """codex P1 on #133: with HEAD as parent, a local commit holding `.env` would have been
    pushed along with the snapshot, as `<snapshot>^:.env`. The snapshot has no parent."""
    repo, bare = github_checkout(tmp_path, fake_github)
    (repo / ".env").write_text("TOKEN=decoy-in-local-history\n")
    (repo / "work.md").write_text("draft\n")
    run_git(repo, "add", ".env", "work.md")
    run_git(repo, "commit", "-q", "-m", "local only, never pushed")
    secret = run_git(repo, "rev-parse", "HEAD:.env")
    local = run_git(repo, "rev-parse", "HEAD")
    ref = await g.publish_snapshot(
        LocalWorkspace(str(repo)), f"{SESSION}/4", github_owner="MathAgents"
    )
    assert ref is not None
    objects = run_git(bare, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    assert secret not in objects.split() and local not in objects.split()
    assert "work.md" in run_git(bare, "ls-tree", "--name-only", ref.sha)
    assert ".env" not in run_git(bare, "ls-tree", "--name-only", ref.sha)


async def test_once_per_name_even_for_concurrent_publications(repo: Path):
    """codex P1 on #133: an existing ref is reused, never replaced, and two publications of
    one name at once end with one commit (the ref is created only if absent)."""
    ws = LocalWorkspace(str(repo))
    first, second = await asyncio.gather(
        g.snapshot_commit(ws, f"{SESSION}/5"), g.snapshot_commit(ws, f"{SESSION}/5")
    )
    assert first and first == second == run_git(repo, "rev-parse", f"refs/zab/turns/{SESSION}/5")
    (repo / "README.md").write_text("changed afterwards\n")
    assert await g.snapshot_commit(ws, f"{SESSION}/5") == first


@pytest.mark.parametrize(
    "origin,owner",
    [
        ("git@github.com-alice:MathAgents/alice.git", "SomeoneElse"),  # not the server's owner
        ("https://gitlab.com/MathAgents/alice.git", "MathAgents"),  # not GitHub
    ],
)
async def test_no_snapshot_unless_origin_is_the_owners_github_repository(
    tmp_path, fake_github, origin, owner
):
    repo, bare = github_checkout(tmp_path, fake_github, origin)
    ws = LocalWorkspace(str(repo))
    assert await g.publish_snapshot(ws, f"{SESSION}/1", github_owner=owner) is None
    assert run_git(repo, "for-each-ref", "refs/zab") == ""  # nothing committed either
    assert run_git(bare, "for-each-ref", "refs/zab") == ""
    no_origin = make_repo(tmp_path / "no-origin")
    no_ws = LocalWorkspace(str(no_origin))
    assert await g.publish_snapshot(no_ws, "s/1", github_owner=owner) is None


async def test_another_organizations_workshop_gets_its_snapshots(tmp_path: Path, fake_github):
    """#693 criterion 2: a VM of a workshop whose `github_org` isn't the example deployment's.
    Its config, as `push_config.py --refserve-owner` writes it, names that organization; the
    snapshot of a checkout of it is then published, where the bridge's default owner, which
    an older push_config left in place, published none."""
    from zab.config import RenderConfig, load_config

    repo = make_repo(tmp_path / "alice")
    bare = fake_github("Example-Org/alice.git")
    run_git(repo, "remote", "add", "origin", "git@github.com-alice:Example-Org/alice.git")
    run_git(repo, "push", "-q", str(bare), "main")
    config = tmp_path / "config.toml"
    config.write_text(
        '[render]\nrefserve_url = "https://files.example.org"\nrefserve_owner = "Example-Org"\n'
    )
    owner = load_config(config).render.refserve_owner
    ws = LocalWorkspace(str(repo))
    default = RenderConfig().refserve_owner
    assert default != owner
    assert await g.publish_snapshot(ws, f"{SESSION}/1", github_owner=default) is None
    ref = await g.publish_snapshot(ws, f"{SESSION}/2", github_owner=owner)
    assert ref is not None and ref.repo == "alice"
    pushed = run_git(bare, "for-each-ref", "--format=%(objectname) %(refname)", "refs/zab")
    assert pushed == f"{ref.sha} refs/zab/turns/{SESSION}/2"


async def test_an_origin_with_several_push_destinations_gets_nothing(tmp_path: Path, fake_github):
    """codex P1 on #133: `git push origin` goes to every push URL, and `config --get` sees only
    the last; another owner's repository listed first must not receive the snapshot."""
    repo, bare = github_checkout(tmp_path, fake_github)
    other = fake_github("SomeoneElse/private.git")
    stranger = "git@github.com-x:SomeoneElse/private.git"
    ours = "git@github.com-alice:MathAgents/alice.git"
    run_git(repo, "config", "--add", "remote.origin.pushurl", stranger)
    run_git(repo, "config", "--add", "remote.origin.pushurl", ours)
    ws = LocalWorkspace(str(repo))
    assert await g.publish_snapshot(ws, f"{SESSION}/6", github_owner="MathAgents") is None
    for remote in (bare, other):
        assert run_git(remote, "for-each-ref", "refs/zab") == ""
    assert run_git(repo, "for-each-ref", "refs/zab") == ""  # nothing committed either
    # two `url` entries without a push URL are refused the same way
    run_git(repo, "config", "--unset-all", "remote.origin.pushurl")
    run_git(repo, "config", "--add", "remote.origin.url", stranger)
    assert await g.publish_snapshot(ws, f"{SESSION}/6", github_owner="MathAgents") is None


@pytest.mark.parametrize("rule", ["pushInsteadOf", "insteadOf"])
async def test_a_url_rewrite_to_another_destination_gets_nothing(tmp_path, fake_github, rule):
    """codex P1 on #133: `git push <url>` still applies url.*.insteadOf / pushInsteadOf, so an
    origin that passes the owner check could be pushed elsewhere; then nothing is published."""
    repo, bare = github_checkout(tmp_path, fake_github)
    other = fake_github("SomeoneElse/alice.git")
    run_git(
        repo,
        "config",
        f"url.git@github.com-x:SomeoneElse/.{rule}",
        "git@github.com-alice:MathAgents/",
    )
    ws = LocalWorkspace(str(repo))
    assert await g.publish_snapshot(ws, f"{SESSION}/7", github_owner="MathAgents") is None
    for remote in (bare, other):
        assert run_git(remote, "for-each-ref", "refs/zab") == ""
    assert run_git(repo, "for-each-ref", "refs/zab") == ""


async def test_an_explicit_pushurl_is_not_rewritten_by_push_instead_of(tmp_path, fake_github):
    """codex P1 (round 6) on #133: git ignores pushInsteadOf for an explicit pushurl when it
    resolves the remote, but applied it to the literal URL the snapshot used to push to. The
    push goes to `origin`, so it lands where the check looked: MathAgents, not SomeoneElse."""
    repo, bare = github_checkout(tmp_path, fake_github)
    other = fake_github("SomeoneElse/alice.git")
    run_git(repo, "config", "remote.origin.pushurl", "git@github.com-alice:MathAgents/alice.git")
    rule = "url.git@github.com-x:SomeoneElse/.pushInsteadOf"
    run_git(repo, "config", rule, "git@github.com-alice:MathAgents/")
    ws = LocalWorkspace(str(repo))
    ref = await g.publish_snapshot(ws, f"{SESSION}/12", github_owner="MathAgents")
    assert ref is not None
    assert run_git(bare, "rev-parse", f"refs/zab/turns/{SESSION}/12") == ref.sha
    assert run_git(other, "for-each-ref", "refs/zab") == ""


async def test_a_failed_push_gives_no_snapshot_and_is_logged(tmp_path: Path, fake_github, caplog):
    repo, _ = github_checkout(tmp_path, fake_github)
    run_git(repo, "remote", "set-url", "origin", "git@github.com-gone:MathAgents/gone.git")
    ws = LocalWorkspace(str(repo))
    with caplog.at_level(logging.WARNING):
        assert await g.publish_snapshot(ws, f"{SESSION}/3", github_owner="MathAgents") is None
    assert "snapshot push of refs/zab/turns/" in caplog.text


async def test_a_push_is_timed_and_a_slow_one_gives_up_with_a_log(
    tmp_path: Path, fake_github, caplog, monkeypatch
):
    """PM on issue #36 (Wed 17:25): the push's wall time is logged at INFO, so the fleet tells
    how close it runs to the bound; a push slower than the bound gives no snapshot (the links
    fall back to GitHub) and says it timed out."""
    repo, bare = github_checkout(tmp_path, fake_github)
    ws = LocalWorkspace(str(repo))
    with caplog.at_level(logging.INFO, logger=g.logger.name):
        assert await g.publish_snapshot(ws, f"{SESSION}/7", github_owner="MathAgents")
    assert f"snapshot push of refs/zab/turns/{SESSION}/7: " in caplog.text
    # the same fake GitHub behind an ssh that takes 5 s to connect, against a 0.5 s bound
    slow = tmp_path / "slow-ssh"
    slow.write_text("#!/bin/sh\nsleep 5\nexec " + g._PUSH_ENV["GIT_SSH_COMMAND"] + ' "$@"\n')
    slow.chmod(0o755)
    monkeypatch.setitem(g._PUSH_ENV, "GIT_SSH_COMMAND", str(slow))
    monkeypatch.setattr(g, "PUSH_TIMEOUT_S", 0.5)
    caplog.clear()
    started = asyncio.get_running_loop().time()
    with caplog.at_level(logging.INFO, logger=g.logger.name):
        assert await g.publish_snapshot(ws, f"{SESSION}/8", github_owner="MathAgents") is None
    assert asyncio.get_running_loop().time() - started < 4  # bounded, not the 5 s connect
    assert f"snapshot push of refs/zab/turns/{SESSION}/8 timed out after " in caplog.text
    assert run_git(bare, "for-each-ref", f"refs/zab/turns/{SESSION}/8") == ""


def test_the_push_is_bounded_and_never_prompts():
    # 120 s, and a total that fits it (PM on issue #36, Wed 17:25: 20 s and 200 MiB did not)
    assert g.PUSH_TIMEOUT_S == 120.0
    assert g.SNAPSHOT_TOTAL_CAP == 50 * 1024 * 1024 >= 2 * g.SNAPSHOT_FILE_CAP
    assert g._PUSH_ENV["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in g._PUSH_ENV["GIT_SSH_COMMAND"]
