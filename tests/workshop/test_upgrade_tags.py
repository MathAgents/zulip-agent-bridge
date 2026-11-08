"""`workshop/adminserver/upgrade.py`'s review and tag (#772), offline: the review that this
copy's deploy.sh pages, the signature through the agent, its check (never git's exit status),
the push, the copy of vN, and the reruns. On the machine of `upgrade_rig`, with real git, a
real ssh-agent and throwaway keys; `origin` stands for the repository the releases are tagged
in, or the public one, whose maintainer's key is `other`."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from .upgrade_rig import Machine, missing_tools

pytestmark = pytest.mark.skipif(bool(missing_tools()), reason=f"needs {missing_tools()}")
V = "v0.3.1"


@pytest.fixture
def machine(tmp_path):
    m = Machine(tmp_path)
    m.copy_v0 = m.copy("v0.3.0")
    m.c = m.release(V)
    m.sha = m.review_sha256(m.c)
    yield m
    m.close()


def run(machine: Machine, typed=f"{V}\n", sha="", env=None):
    return machine.run(V, "--commit", machine.c, "--review-sha256", sha or machine.sha,
                       copy=machine.copy_v0, input=typed, env=env)  # fmt: skip


def said(done) -> str:
    return done.stdout + done.stderr


def tag_of(machine: Machine, where: Path) -> str:
    """The tag object of refs/tags/vN in `where`, or ""."""
    return machine.sh("git", "-C", where, "rev-parse", "--verify", "--quiet",
                      f"refs/tags/{V}").stdout.strip()  # fmt: skip


def transcript(machine: Machine) -> str:
    return (machine.admin / f"upgrades/{V}/transcript.txt").read_text()


def verifies(machine: Machine, key: Path) -> bool:
    signers = f"gpg.ssh.allowedSignersFile={machine.etc / 'allowed_signers'}"
    done = machine.sh("git", "-C", machine.clone, "-c", signers, "tag", "-v", V)
    return done.returncode == 0 and f"with ED25519 key {machine.fingerprint(key)}" in done.stderr


def test_a_run_reviews_signs_pushes_and_copies_and_a_rerun_changes_nothing(machine):
    done = run(machine)
    assert done.returncode == 0, said(done)
    out = done.stdout
    assert f"zab admin release review: v0.3.0..{machine.c[:12]}" in out  # paged, here cat
    assert f"the review's SHA-256 is the release message's, {machine.sha}.\n" in out
    assert f"Type {V} to sign it on {machine.c[:12]}: " in out
    obj = tag_of(machine, machine.clone)
    assert obj and tag_of(machine, machine.origin) == obj and verifies(machine, machine.key)
    assert f"pushed: origin's {V} is {obj}\n" in out
    copy = machine.home / f"zab-{V}"
    assert machine.git("rev-parse", "HEAD", where=copy) == machine.c
    again = run(machine, typed="")
    assert again.returncode == 0, said(again)
    assert "zab admin release review" not in again.stdout and "Type " not in again.stdout
    assert f"{machine.sha}: compared again, its review not paged, since {V}'s tag" in again.stdout
    assert f": done ({copy} is its copy)\n" in again.stdout
    assert f"the review's SHA-256, {machine.sha},\nwhich this run compared" in again.stdout
    assert tag_of(machine, machine.clone) == obj == tag_of(machine, machine.origin)
    other = "1" * 64  # a rerun with another SHA-256 than the review's is compared too
    wrong = run(machine, typed="", sha=other)
    assert wrong.returncode == 1 and other not in wrong.stdout, said(wrong)
    assert f"diff has SHA-256 {machine.sha}, not {other}, the release message's: check" in (
        wrong.stderr
    )


def test_a_tag_of_this_clones_on_another_commit_stops_the_run_before_the_review(machine):
    """A vN this clone signed, never pushed, on another commit than the release message's: the
    run stops, naming both commits and the line that deletes the tag, and deletes nothing."""
    other = machine.release(V, parent="main")  # off main, and it says 0.3.1 too
    machine.git("tag", "side", other)  # so that the clone's fetch brings the commit
    machine.git("fetch", "-q", "origin", "--tags", where=machine.clone)
    machine.git("tag", "-d", "side")
    machine.git("tag", "-d", "side", where=machine.clone)
    machine.sign(V, other, machine.key, where=machine.clone)
    obj = tag_of(machine, machine.clone)
    done = run(machine)
    assert done.returncode == 1, said(done)
    assert f"stopped: this clone's {V} verifies on {other}, not on {machine.c}, the release" in (
        done.stderr
    )
    assert f"never pushed: git -C {machine.clone} tag -d {V}\n" in done.stderr
    assert "zab admin release review" not in done.stdout
    assert tag_of(machine, machine.clone) == obj and tag_of(machine, machine.origin) == ""


@pytest.mark.parametrize("how", ["another SHA-256", "not typed"])
def test_nothing_is_signed_unless_the_review_matches_and_is_confirmed(machine, how):
    other = "0" * 64
    done = run(machine, sha=other if how == "another SHA-256" else "", typed="yes\n")
    assert done.returncode == 1, said(done)
    if how == "another SHA-256":
        assert f"stopped: this review's diff has SHA-256 {machine.sha}, not {other}" in done.stderr
        assert "Type " not in done.stdout
    else:
        assert done.stderr.endswith("stopped: not confirmed\n")
        log = transcript(machine)  # whether it matched, never what was typed: a passphrase?
        assert "(typed: something else)\n" in log and "(typed: yes)" not in log
    assert tag_of(machine, machine.clone) == tag_of(machine, machine.origin) == ""


#: git, but a `tag -s` that fails to sign, as when the agent lacks the key and ssh-keygen has no
#: private file to fall back on (#772): with `unsigned`, an annotated tag without a signature
#: and exit 0, as git 2.43 does; otherwise no tag and exit 1, as a newer git does.
FAILING_GIT = """#!{python}
import os, sys
argv = sys.argv[1:]
if "tag" in argv and "-s" in argv[argv.index("tag"):]:
    if not {unsigned}:
        sys.exit("error: No private key found for public key")
    argv[argv.index("-s", argv.index("tag"))] = "-a"
os.execv({git!r}, [{git!r}, *argv])
"""


def stand_in(machine: Machine, name: str, text: str) -> dict[str, str]:
    """`name` on PATH ahead of the real one: the environment for a run that uses it."""
    stub = machine.tmp / "stub"
    stub.mkdir(exist_ok=True)
    (stub / name).write_text(text)
    (stub / name).chmod(0o755)
    return {"PATH": f"{stub}:{machine.env['PATH']}"}


@pytest.mark.parametrize(
    ("unsigned", "why", "code"),
    [
        (True, "not signed with an SSH key: the tag was deleted, never pushed", 0),
        (False, "no tag made (git tag -s: error: No private key found for public key)", 1),
    ],
)
def test_a_failed_signature_leaves_no_tag_and_the_run_stops(machine, unsigned, why, code):
    """Whatever git's exit status: the tag is checked, never git's word."""
    text = FAILING_GIT.format(python=sys.executable, git=shutil.which("git"), unsigned=unsigned)
    done = run(machine, env=stand_in(machine, "git", text))
    assert done.returncode == 1, said(done)
    assert f"stopped: {V}: {why}. The agent holds the key alone" in done.stderr, said(done)
    transcript = (machine.admin / f"upgrades/{V}/transcript.txt").read_text()
    assert f"tag -s {V} -m 'zab {V}' {machine.c}\n  exit {code}\n" in transcript  # as git said
    assert tag_of(machine, machine.clone) == tag_of(machine, machine.origin) == ""


def test_whichever_way_git_fails_to_sign_no_tag_is_left_and_the_run_stops(machine):
    """The real git, with the agent empty and the private file away (`No private key found
    for public key`): git 2.43 makes an unsigned tag and exits 0, a newer one makes no tag and
    exits 1. A stand-in `ssh-add -l` says the agent holds the key, so the driver signs."""
    machine.key.rename(machine.tmp / "away")
    listed = f"256 {machine.fingerprint(machine.key)} zab-deployer (ED25519)"
    real = shutil.which("ssh-add")
    lying = f'#!/bin/sh\n[ "$1" = -l ] && echo "{listed}" && exit 0\nexec {real} "$@"\n'
    done = run(machine, env={**machine.agent(), **stand_in(machine, "ssh-add", lying)})
    assert done.returncode == 1, said(done)
    assert re.search(rf"stopped: {V}: (not signed with an SSH key: the tag was deleted, never "
                     r"pushed|no tag made \(git tag -s: .*No private key found)", done.stderr), (
        said(done)
    )  # fmt: skip
    assert tag_of(machine, machine.clone) == tag_of(machine, machine.origin) == ""


#: FAILING_GIT's unsigned tag and exit 0, with the error git 2.43 prints then, and a `tag -d`
#: that fails, as on a ref's lock file left behind (#895)
UNDELETABLE_GIT = """#!{python}
import os, sys
argv = sys.argv[1:]
if "tag" in argv:
    if "-d" in argv[argv.index("tag"):]:
        sys.exit("error: cannot lock ref 'refs/tags/{v}'")
    if "-s" in argv[argv.index("tag"):]:
        print("error: No private key found for public key", file=sys.stderr, flush=True)
        argv[argv.index("-s", argv.index("tag"))] = "-a"
os.execv({git!r}, [{git!r}, *argv])
"""


def test_a_failed_signatures_tag_that_isnt_deleted_is_named_and_a_rerun_deletes_it(machine):
    """#895: the delete after a failed signature is checked by what is left, never assumed. The
    run says it failed, with git's error and the tag still there; a rerun deletes it first. The
    signing's error comes before the delete's command, never read as its own (#910)."""
    text = UNDELETABLE_GIT.format(python=sys.executable, git=shutil.which("git"), v=V)
    done = run(machine, env=stand_in(machine, "git", text))
    assert done.returncode == 1, said(done)
    left = tag_of(machine, machine.clone)
    assert left and tag_of(machine, machine.origin) == ""
    assert (
        f"stopped: {V}: not signed with an SSH key (git tag -s: error: No private key found for"
        f" public key), and its delete failed (git tag -d: error: cannot lock ref"
        f" 'refs/tags/{V}'): this clone's {V} is still {left}, never pushed."
        f" Delete it (git -C {machine.clone} tag -d {V}). The agent holds the key alone"
    ) in done.stderr, said(done)
    assert "was deleted" not in done.stderr
    again = run(machine)
    assert again.returncode == 0, said(again)
    assert f"{V}: not signed with an SSH key: deleting this clone's {V} (it was never pushed)" in (
        again.stdout
    )
    assert tag_of(machine, machine.clone) == tag_of(machine, machine.origin) != left


#: git, logging which agent each fetch, ls-remote, push and signing gets
RECORDING_GIT = """#!{python}
import os, sys
what = [a for a in sys.argv[1:] if a in ("fetch", "ls-remote", "push", "-s")]
if what:
    with open({log!r}, "a") as log:
        log.write(what[0] + " " + os.environ.get("SSH_AUTH_SOCK", "(none)") + "\\n")
os.execv({git!r}, [{git!r}, *sys.argv[1:]])
"""


def test_only_the_signing_gets_the_drivers_own_agent(machine):
    """What reaches origin authenticates as it would without the driver: with this terminal's
    agent (here, one holding another key too), never the driver's, which holds the dedicated
    key alone and would be all an SSH origin is offered."""
    log = machine.tmp / "agents"
    text = RECORDING_GIT.format(python=sys.executable, log=str(log), git=shutil.which("git"))
    terminal = machine.agent(machine.key, machine.other)
    done = run(machine, env={**terminal, **stand_in(machine, "git", text)})
    assert done.returncode == 0 and "the driver uses its own" in done.stdout, said(done)
    used: dict[str, set[str]] = {}
    for line in log.read_text().splitlines():
        used.setdefault(line.split()[0], set()).add(line.split()[1])
    assert used["fetch"] == used["ls-remote"] == used["push"] == {terminal["SSH_AUTH_SOCK"]}
    own = re.search(r"^\$ ssh-agent -D -a (\S+)$", done.stdout + transcript(machine), re.M)[1]
    assert used["-s"] == {own}


def test_another_keys_signature_is_deleted_and_the_run_stops(machine):
    machine.conf.write_text(f"key={machine.other}\nclone={machine.clone}\nadmin={machine.admin}\n")
    done = run(machine)
    assert done.returncode == 1, said(done)
    assert f"stopped: {V}: not signed by a key of {machine.etc / 'allowed_signers'}" in done.stderr
    assert tag_of(machine, machine.clone) == tag_of(machine, machine.origin) == ""


def test_origins_tag_of_another_signer_stays_and_this_clone_keeps_its_own(machine):
    """The public repository (BOOTSTRAP §9): origin's vN is its maintainer's. The clone's copy
    of it is deleted, its own signed in its place, and never pushed; origin's never moves."""
    machine.sign(V, machine.c, machine.other)
    theirs = tag_of(machine, machine.origin)
    done = run(machine)
    assert done.returncode == 0, said(done)
    assert f"{V}: not signed by a key of" in done.stdout  # the fetched copy, deleted
    signer = f"signed by {machine.fingerprint(machine.other)}"
    assert f"origin has another {V} of {machine.c[:12]}, {signer}, which stays as it" in (
        done.stdout
    )
    assert "Check that fingerprint against the one SECURITY.md publishes" in done.stdout
    assert tag_of(machine, machine.origin) == theirs != tag_of(machine, machine.clone)
    assert verifies(machine, machine.key)
    again = run(machine, typed="")
    assert again.returncode == 0 and "Type " not in again.stdout, said(again)


@pytest.mark.parametrize("case", ["unsigned", "a bad signature"])
def test_origins_tag_with_no_fingerprint_to_check_is_called_an_anomaly(machine, case):
    """#895: origin's other vN of C, with no SSH signature or one that doesn't check, has no
    fingerprint to check against SECURITY.md's. The run says what it is, and that in a
    repository you tag yourself it is an anomaly; origin's stays, this clone keeps its own."""
    if case == "unsigned":
        machine.git("tag", "-a", "-m", f"zab {V}", V, machine.c)
    else:  # signed, then its message changed
        machine.sign(V, machine.c, machine.other)
        text = machine.git("cat-file", "tag", V).replace(f"zab {V}", f"zab {V}!") + "\n"
        bad = machine.sh("git", "-C", machine.origin, "mktag", input=text)
        assert bad.returncode == 0, bad.stderr
        machine.git("update-ref", f"refs/tags/{V}", bad.stdout.strip())
    theirs = tag_of(machine, machine.origin)
    done = run(machine)
    assert done.returncode == 0, said(done)
    signer, how = {
        "unsigned": ("with no SSH signature", "has no SSH signature"),
        "a bad signature": ("with a signature that doesn't check", "has a signature that doesn't"),
    }[case]
    assert f"origin has another {V} of {machine.c[:12]}, {signer}, which stays as it" in (
        done.stdout
    )
    assert f"origin's tag {how}" in done.stdout and "no fingerprint to check" in done.stdout
    assert "in a repository you tag yourself such a tag is an anomaly worth" in done.stdout
    assert "Check that fingerprint" not in done.stdout
    assert tag_of(machine, machine.origin) == theirs != tag_of(machine, machine.clone)
    assert verifies(machine, machine.key)


def test_origins_tag_of_another_commit_stops_the_run_before_the_review(machine):
    machine.sign(V, machine.git("rev-parse", "v0.3.0^{commit}"), machine.key)
    done = run(machine)
    assert done.returncode == 1, said(done)
    assert f"stopped: origin's {V} names " in done.stderr and "check the release message" in (
        done.stderr
    )
    assert "zab admin release review" not in done.stdout


def test_a_push_that_fails_keeps_the_signed_tag_and_a_rerun_pushes_it(machine):
    hook = machine.origin / ".git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho no pushes today >&2\nexit 1\n")
    hook.chmod(0o755)
    done = run(machine)
    assert done.returncode == 1 and f"stopped: git push origin {V}: " in done.stderr, said(done)
    assert "It is signed here: run this again to push it" in done.stderr
    obj = tag_of(machine, machine.clone)
    assert obj and tag_of(machine, machine.origin) == ""
    hook.unlink()
    again = run(machine, typed="")
    assert again.returncode == 0, said(again)
    assert "signed: " not in again.stdout and f"pushed: origin's {V} is {obj}\n" in again.stdout
    assert tag_of(machine, machine.origin) == obj


def test_the_drivers_fetch_leaves_no_maintenance_running_beside_its_worktree_add(machine):
    """The maintenance that git starts after a fetch prunes worktrees in the background from
    git 2.54 on, and can remove the one that `git worktree add` is making (#920): the driver's
    fetch turns it off, whatever the clone's configuration (the rig's is for its own commands)."""
    machine.git("config", "--unset", "maintenance.auto", where=machine.clone)
    trace = machine.tmp / "trace"
    done = run(machine, env={"GIT_TRACE": str(trace)})
    assert done.returncode == 0, said(done)
    lines = trace.read_text().splitlines()
    assert any("git fetch --no-tags" in line for line in lines)
    assert any("git worktree add" in line for line in lines)
    assert [line for line in lines if re.search("maintenance run|gc --auto", line)] == []


@pytest.mark.parametrize("case", ["changes", "another commit"])
def test_a_copy_that_is_not_clean_at_c_stops_the_run(machine, case):
    copy = machine.home / f"zab-{V}"
    machine.git("fetch", "-q", "origin", where=machine.clone)
    at = machine.c if case == "changes" else machine.git("rev-parse", "v0.3.0^{commit}")
    machine.git("worktree", "add", "-q", "--detach", copy, at, where=machine.clone)
    if case == "changes":
        (copy / "notes.txt").write_text("mine\n")
    done = run(machine)
    assert done.returncode == 1, said(done)
    why = "has changes (git status)" if case == "changes" else f"is not at {machine.c}"
    assert done.stderr.endswith(f"stopped: {copy} {why}: move it aside, then run this again\n")


def test_a_home_reached_through_a_link_finds_its_copy_again(machine):
    """git gives resolved paths; the copy's path is compared resolved too."""
    link = machine.tmp / "home-link"
    link.symlink_to(machine.home)
    assert run(machine, env={"HOME": str(link)}).returncode == 0
    again = run(machine, typed="", env={"HOME": str(link)})
    assert again.returncode == 0, said(again)
    assert f": done ({link / f'zab-{V}'} is its copy)\n" in again.stdout


def test_something_else_at_the_copys_path_stops_the_run(machine):
    (machine.home / f"zab-{V}").mkdir()
    (machine.home / f"zab-{V}" / "notes.txt").write_text("mine\n")
    done = run(machine)
    assert done.returncode == 1, said(done)
    assert f"stopped: {machine.home / f'zab-{V}'} is not a checkout of {machine.clone}: move" in (
        done.stderr
    )
    assert tag_of(machine, machine.origin) == tag_of(machine, machine.clone) != ""
