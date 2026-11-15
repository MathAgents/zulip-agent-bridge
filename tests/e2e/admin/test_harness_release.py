"""Test releases, offline with real git and ssh-keygen (issue #356): version commits, signed
and unsigned tags, a renamed ref, the version order, and how deploy.sh is called; the public
repository's exported releases and the deploy clone (#958)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from e2e.admin.harness import rig as rigs
from e2e.admin.harness import upgrade
from e2e.admin.harness.gate import Gate
from e2e.admin.harness.release import (
    IDENTITY,
    Releases,
    dropped,
    ensure_keys,
    key,
    local,
    next_version,
    older_version,
    typed,
    write_ssh,
)
from e2e.harness.vm import Result

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("ssh-keygen")), reason="needs git and ssh-keygen"
)


def git(repo, *args, check=True):
    argv = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args]
    return subprocess.run(argv, capture_output=True, text=True, check=check)  # noqa: S603


@pytest.fixture
def made(tmp_path):
    return _made(tmp_path)


def _made(tmp_path):
    """A checkout on `main`, as zab's own is, and the rig's release repository cloned from it:
    the clone's HEAD names `main` too, so no test may fetch into that branch (#1042). It needs
    no fetch: the clone shares the checkout's objects (`--shared`), a later commit's too."""
    checkout = tmp_path / "checkout"
    (checkout / "src" / "zab").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname = "zab"\nversion = "0.2.3"\n')
    (checkout / "src" / "zab" / "__init__.py").write_text('"""zab."""\n\n__version__ = "0.2.3"\n')
    git(checkout, "init", "-q", "-b", "main")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "base")
    rig = rigs.load({"HOME": str(tmp_path)})
    ensure_keys(Gate(dry_run=False, out=lambda line: None), rig)
    rel = Releases(rig, checkout=checkout)
    rel.ensure_repo()
    signers = tmp_path / "allowed_signers"
    pub = (rig.keys / "signing.pub").read_text().strip()
    signers.write_text(f'e2e@example.org namespaces="git" {pub}\n')
    return rel, rel.commit_of("HEAD"), signers


def verify(rel, signers, version):
    return git(rel.rig.repo, "-c", f"gpg.ssh.allowedSignersFile={signers}", "verify-tag",
               version, check=False)  # fmt: skip


def test_a_signed_release_of_the_commit_under_test(made):
    rel, base, signers = made
    assert rel.base_version(base) == "0.2.3"
    version = next_version("0.2.3", None)
    assert version == "v0.2.4rc1"
    commit = rel.release(base, version)
    assert rel.git("rev-parse", f"{commit}^") == base
    assert 'version = "0.2.4rc1"' in rel.git("show", f"{commit}:pyproject.toml")
    assert '__version__ = "0.2.4rc1"' in rel.git("show", f"{commit}:src/zab/__init__.py")
    changed = rel.git("diff", "--name-only", base, commit).split()
    assert changed == ["pyproject.toml", "src/zab/__init__.py"]  # the version only
    assert verify(rel, signers, version).returncode == 0
    assert rel.has_tag(version) and not rel.has_tag("v9.9.9")
    rel.release(base, version)  # an earlier run's tag of that name is replaced
    assert verify(rel, signers, version).returncode == 0
    assert git(rel.checkout, "tag").stdout == ""  # no test tag in the zab repository


def test_a_release_carries_files_of_any_bytes_as_they_are(made):
    """#963: A10b's good rc carries files besides its version, each written whole, its bytes
    as they are (a CR, controls, a line that isn't UTF-8): a file it adds and one it replaces.
    The version's own two files are its alone."""
    rel, base, _ = made
    (rel.checkout / "CHANGELOG.md").write_text("# Changelog\n\n## 0.2.3\n")
    git(rel.checkout, "add", "-A")
    git(rel.checkout, "commit", "-q", "-m", "a changelog")
    base = rel.commit_of("HEAD")
    added = b"cr\r\nesc \x1b[0m\n\xff\xe2\x80\n"
    files = {"workshop/adminserver/a.md": added, "CHANGELOG.md": b"x"}
    commit = rel.release(base, "v0.2.4rc1", files=files)
    for path, data in files.items():
        argv = ["git", "-C", str(rel.rig.repo), "cat-file", "blob", f"{commit}:{path}"]
        blob = subprocess.run(argv, capture_output=True, check=True)  # noqa: S603
        assert blob.stdout == data, path
    changed = rel.git("diff", "--name-only", base, commit).split()
    assert changed == ["CHANGELOG.md", "pyproject.toml", "src/zab/__init__.py",
                       "workshop/adminserver/a.md"]  # fmt: skip
    assert 'version = "0.2.4rc1"' in rel.git("show", f"{commit}:pyproject.toml")
    with pytest.raises(ValueError, match="the version's own files"):
        rel.release(base, "v0.2.4rc2", files={"pyproject.toml": b"x"})


def test_the_bad_tags_of_a10(made):
    rel, base, signers = made
    rel.release(base, "v0.2.4rc1", signer=None)
    assert "BEGIN SSH SIGNATURE" not in rel.git("cat-file", "tag", "v0.2.4rc1")
    rel.release(base, "v0.2.4rc1", signer="unknown")
    assert "BEGIN SSH SIGNATURE" in rel.git("cat-file", "tag", "v0.2.4rc1")
    assert verify(rel, signers, "v0.2.4rc1").returncode != 0
    rel.release(base, "v0.2.4rc1")
    obj = rel.git("rev-parse", "refs/tags/v0.2.4rc1")
    rel.alias("v0.2.4rc2", obj)
    assert rel.git("rev-parse", "refs/tags/v0.2.4rc2") == obj
    assert "\ntag v0.2.4rc1\n" in "\n" + rel.git("cat-file", "tag", "v0.2.4rc2")


def test_a_public_release_is_the_export_of_the_private_one(made):
    """#958: the public commit is the private tree less the export's DROP, one commit on the
    public main with its Exported-from trailer, tagged in the public repository; the public
    repository holds no private commit."""
    rel, base, signers = made
    checkout = rel.checkout
    (checkout / "tools" / "publish").mkdir(parents=True)
    (checkout / "tools" / "publish" / "export.py").write_text(
        '"""The tool."""\n\nDROP = (\n    "notes/",  # a directory\n    "AGENTS.md",\n)\n'
    )
    (checkout / "notes").mkdir()
    (checkout / "notes" / "private.md").write_text("private\n")
    (checkout / "AGENTS.md").write_text("private\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "with private files")
    rel.ensure_repo()
    base = rel.commit_of("HEAD")
    first = rel.release(base, "v0.2.4rc1")
    p1 = rel.export("v0.2.4rc1", first)
    second = rel.release(base, "v0.2.4rc2")
    p2 = rel.export("v0.2.4rc2", second, signer=None)
    files = rel.public_git("ls-tree", "-r", "--name-only", p2).split()
    assert "notes/private.md" not in files and "AGENTS.md" not in files
    assert "tools/publish/export.py" in files  # this tool's own DROP isn't the fixture's
    assert rel.git("diff", "--name-only", second, p2).split() == ["AGENTS.md", "notes/private.md"]
    assert rel.public_git("rev-list", "--all").split() == [p2, p1]  # public history alone
    assert rel.public_git("log", "-1", "--format=%B", p2).strip() == (
        f"zab v0.2.4rc2\n\nExported-from: {second}"
    )
    assert rel.public_git("rev-parse", "refs/heads/main") == p2
    good = git(rel.public, "-c", f"gpg.ssh.allowedSignersFile={signers}", "verify-tag",
               "v0.2.4rc1", check=False)  # fmt: skip
    assert good.returncode == 0, good.stderr
    assert "BEGIN SSH SIGNATURE" not in rel.public_git("cat-file", "tag", "v0.2.4rc2")
    assert rel.has_public_tag("v0.2.4rc1") and not rel.has_public_tag("v0.2.4rc3")
    assert git(checkout, "tag").stdout == ""  # no test tag in the zab repository
    clone = rel.deploy_clone("v0.2.4rc1", rel.rig.repo.parent / "clone")
    assert git(clone, "remote").stdout == ""  # nothing is ever pushed from it
    tags = git(clone, "for-each-ref", "--format=%(refname:short) %(*objectname)", "refs/tags")
    assert sorted(tags.stdout.split("\n")[:-1]) == sorted(
        [f"v0.2.4rc1 {first}", f"v0.2.4rc2 {p2}"]  # the private base, and the public tags
    ), tags.stdout


def _commit(rel, files: dict[str, str], message: str) -> str:
    """`files` written into the checkout and committed, then fetched into the rig's repository:
    a new commit under test."""
    for name, text in files.items():
        (rel.checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (rel.checkout / name).write_text(text)
    git(rel.checkout, "add", "-A")
    git(rel.checkout, "commit", "-q", "-m", message)
    return rel.commit_of("HEAD")


def test_a_fresh_export_starts_the_public_history_and_only_exports_of_the_base_pass(made):
    """#977's review, B1: A10's export starts the public history afresh, as a root commit, and
    what a push of two tags would send is then this run's exports alone, each a test release of
    the commit under test (`exports_of`). An earlier run's export of another commit, or a commit
    that is no export, is refused, before anything is pushed."""
    rel, earlier, _ = made
    old = rel.export("v0.2.4rc1", rel.release(earlier, "v0.2.4rc1"))  # an earlier run's
    base = _commit(rel, {"later.txt": "the commit under test\n"}, "later")
    running = rel.export("v0.2.4rc2", rel.release(base, "v0.2.4rc2"), fresh=True)  # A10's
    assert rel.public_git("rev-list", "--parents", "-n", "1", running).split() == [running]
    assert rel.public_git("rev-parse", "refs/heads/main") == running
    unsigned = rel.export("v0.2.4rc3", rel.release(base, "v0.2.4rc3"), signer=None)  # A10b's
    good = rel.export("v0.2.4rc3", rel.release(base, "v0.2.4rc3"))
    refs = ("refs/tags/v0.2.4rc2", "refs/tags/v0.2.4rc3")
    assert {public for public, _ in rel.exports_of(base, *refs)} == {good, unsigned, running}
    for _, private in rel.exports_of(base, *refs):
        assert rel.git("rev-parse", f"{private}^") == base
    with pytest.raises(ValueError, match=rf"{old[:12]} is an export of .*, no test release of"):
        rel.exports_of(base, "refs/tags/v0.2.4rc1", *refs)
    with pytest.raises(ValueError, match="run A10 first"):
        rel.exports_of(earlier, *refs)
    tree = rel.public_git("rev-parse", f"{good}^{{tree}}")
    stray = rel.public_git(*IDENTITY, "commit-tree", tree, "-p", good, "-m", "no export")
    rel.public_git("update-ref", "refs/tags/v0.2.4rc4", stray)
    with pytest.raises(ValueError, match=rf"{stray[:12]} is no export of a test release"):
        rel.exports_of(base, "refs/tags/v0.2.4rc4")


@pytest.mark.parametrize("bad", ["tree", "blob", "unchecked", "nested"])
def test_a_ref_that_isnt_a_checked_export_or_its_tag_is_refused(made, tmp_path, bad):
    """#977's review round 2, N1: `rev-list` lists nothing for a tag of a tree or a blob, so
    each ref must itself be one of the commits checked, or a tag of one. A tag of a tree, a tag
    of a blob, a lightweight tag at a commit that is no export, and a tag of a tag are each
    refused, with or beside the good refs, and `publish` then pushes nothing and asks GitHub
    nothing."""
    rel, _, _ = made
    base = _commit(rel, {"later.txt": "the commit under test\n"}, "later")
    rel.export("v0.2.4rc1", rel.release(base, "v0.2.4rc1"), fresh=True)
    good = rel.export("v0.2.4rc2", rel.release(base, "v0.2.4rc2"))
    refs = ("refs/tags/v0.2.4rc1", "refs/tags/v0.2.4rc2")
    assert len(rel.exports_of(base, *refs)) == 2
    tree = rel.public_git("rev-parse", f"{good}^{{tree}}")  # the rig's identity, never the user's
    lightweight = rel.public_git(*IDENTITY, "commit-tree", tree, "-m", "no export")
    target = {
        "tree": rel.public_git("rev-parse", f"{good}^{{tree}}"),
        "blob": rel.public_git("rev-parse", f"{good}:pyproject.toml"),
        "nested": "refs/tags/v0.2.4rc2",
    }
    if bad == "unchecked":
        rel.public_git("update-ref", "refs/tags/v9.9.9", lightweight)
    else:
        rel.public_git(*IDENTITY, "tag", "-a", "--no-sign", "-m", bad, "v9.9.9", target[bad])
    why = {
        "tree": "is a tag of a tree",
        "blob": "is a tag of a blob",
        "unchecked": "is no export of a test release",
        "nested": "is a tag of a tag",
    }[bad]
    for asked in (("refs/tags/v9.9.9",), (*refs, "refs/tags/v9.9.9")):
        with pytest.raises(ValueError, match=f"{why}.*nothing pushed"):
            rel.exports_of(base, *asked)
    runs = []
    with pytest.raises(ValueError, match=why):
        upgrade.publish(rel, "MathAgents/zab-test-release-public", upgrade.Token("t" * 30), base,
                        (*refs, "refs/tags/v9.9.9"), tmp_path, run=lambda *a, **k: runs.append(a),
                        scan=lambda tree, where: [])  # fmt: skip
    assert runs == []  # neither GitHub nor a push


def test_an_unsigned_tag_stays_unsigned_whatever_the_users_git_config(made, monkeypatch):
    """#961's review, N4: with `tag.gpgSign` on in the user's configuration, `tag -a` would sign
    with the user's own key, a deployer's, and A10b would push that tag to the test release
    repository; `--no-sign` keeps the unsigned case unsigned, and no other key than the rig's
    is ever named."""
    rel, base, _ = made
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "tag.gpgSign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.signingkey")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "/nonexistent/the-users-own-key")
    commit = rel.release(base, "v0.2.4rc1", signer=None)
    assert "BEGIN" not in rel.git("cat-file", "tag", "v0.2.4rc1")
    rel.export("v0.2.4rc1", commit, signer=None)
    assert "BEGIN" not in rel.public_git("cat-file", "tag", "v0.2.4rc1")


@pytest.mark.parametrize("default", ["", "main", "master"], ids=["unset", "main", "master"])
def test_the_users_default_branch_changes_nothing(tmp_path, monkeypatch, default):
    """#1042: with `init.defaultBranch = main` in the user's global git config, the fixture's
    checkout and so the rig's clone of it were on `main`, the tests' fetch into that branch was
    refused, and seven tests failed, the export's suite with them. Unset, `main` or `master`,
    the setting changes nothing now: the checkout, the clone and the public repository are on
    `main`, and a commit made after the clone is released, exported and deployed, unfetched."""
    config = tmp_path / "gitconfig"
    config.write_text(f"[init]\n\tdefaultBranch = {default}\n" if default else "")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    rel, _, _ = _made(tmp_path)
    for repo in (rel.checkout, rel.rig.repo):
        assert git(repo, "symbolic-ref", "HEAD").stdout == "refs/heads/main\n"
    base = _commit(rel, {"later.txt": "made after the clone\n"}, "later")
    public = rel.export("v0.2.4rc1", rel.release(base, "v0.2.4rc1"), fresh=True)
    assert rel.public_git("symbolic-ref", "HEAD") == "refs/heads/main"
    clone = rel.deploy_clone("v0.2.4rc1", tmp_path / "clone")
    assert git(clone, "rev-parse", "HEAD").stdout.strip() == public


def test_the_drop_list_is_read_without_running_the_tool():
    assert dropped('DROP = ("a/", "b.md")  # comment\nOTHER = ("c",)\n') == ("a/", "b.md")
    assert dropped("import os\nX = 1\n") == ()


def test_versions_follow_deploy_sh_order():
    assert key("v0.2.4rc9") < key("v0.2.4") < key("v0.2.5rc1")
    assert next_version("0.2.3", "v0.2.4rc1") == "v0.2.4rc2"
    assert next_version("0.2.3", "v0.2.4") == "v0.2.5rc1"
    assert next_version("0.3.0rc2", None) == "v0.3.0rc3"
    assert next_version("0.3.0", "v0.2.4rc7") == "v0.3.1rc1"  # a newer commit: its own
    assert older_version("v0.2.4rc2") == "v0.2.4rc1"
    assert older_version("v0.2.4") == "v0.2.4rc0"
    assert older_version("v0.2.4rc0") == "v0.2.3"
    with pytest.raises(ValueError):
        older_version("v0.2.0rc0")
    with pytest.raises(ValueError):
        key("main")


def test_deploy_sh_is_called_as_a_deployer_would(tmp_path):
    rig = rigs.load({"HOME": str(tmp_path)})
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        err = "  SHA-256 " + "a" * 64 + "; release tree " + "b" * 64 + "\n"
        return Result(1, "", err + "deploy.sh: v0.2.4rc1: not signed with an SSH key\n")

    rel = Releases(rig, type_=run)
    done = rel.deploy(tmp_path / "deploy.sh", "v0.2.4rc1", "--bootstrap", out=tmp_path)
    argv, kw = calls[0]
    assert argv[:2] == ["bash", str(tmp_path / "deploy.sh")] and argv[-1] == "v0.2.4rc1"
    assert argv[argv.index("--host") + 1] == "zab-test-admin"
    assert argv[argv.index("--secrets") + 1] == str(rig.secrets)
    assert kw["answer"] == "v0.2.4rc1\n"  # typed at deploy.sh's confirmation, when it asks
    assert kw["prompt"] == "Type v0.2.4rc1 to "
    assert kw["env"]["ZAB_DEPLOY_SSH"] == str(rig.ssh_wrapper)
    assert done.diff_sha256 == "a" * 64 and not done.ok
    assert done.why() == "deploy.sh: v0.2.4rc1: not signed with an SSH key"
    rel.deploy(tmp_path / "deploy.sh", "v0.2.4rc1", out=tmp_path, repo=tmp_path / "clone")
    argv, _ = calls[1]
    assert argv[argv.index("--repo") + 1] == str(tmp_path / "clone")  # a deploy clone (#958)


def test_the_ssh_config_pins_the_server(tmp_path):
    rig = rigs.load({"HOME": str(tmp_path)})
    rig.dir.mkdir(parents=True)
    write_ssh(rig, "198.51.100.5")
    config = rig.ssh_config.read_text()
    assert "HostName 198.51.100.5" in config and "StrictHostKeyChecking yes" in config
    assert f'UserKnownHostsFile "{rig.known_hosts}"' in config and "ForwardAgent no" in config
    assert rig.ssh_wrapper.read_text() == f"#!/bin/sh\nexec ssh -F '{rig.ssh_config}' \"$@\"\n"
    bad = rigs.load({"HOME": str(tmp_path / "it's")})
    with pytest.raises(ValueError, match="quotes"):
        write_ssh(bad, "198.51.100.5")


#: deploy.sh's shape (Codex round 1 on #363): an `ssh` (here `cat`, which reads its standard
#: input as ssh does) before the prompt, the prompt on stderr without a newline, `read`, then
#: another reader after it.
CONFIRM = r"""
timeout 2 cat > "$1/before" || true
printf 'Type %s to deploy it: ' "$2" >&2
read -r answer || { echo "deploy.sh: not confirmed" >&2; exit 1; }
[ "$answer" = "$2" ] || { echo "deploy.sh: not confirmed" >&2; exit 1; }
cat > "$1/after"
seq 1 50000
echo "confirmed $answer"
"""


def test_the_confirmation_is_typed_only_at_the_prompt(tmp_path):
    argv = ["bash", "-c", CONFIRM, "bash", str(tmp_path), "v0.2.4rc1"]
    done = typed(
        argv, answer="v0.2.4rc1\n", prompt="Type v0.2.4rc1 to ", env={"PATH": "/usr/bin:/bin"}
    )
    assert done.ok, done.stderr
    assert done.stdout.endswith("50000\nconfirmed v0.2.4rc1\n")  # a large stdout drained too
    assert (tmp_path / "before").read_text() == ""  # the earlier reader took nothing
    assert (tmp_path / "after").read_text() == ""  # and the later one read EOF, not a hang
    never = typed(["bash", "-c", "echo deploy.sh: refused >&2; exit 1"], answer="v\n",
                  prompt="Type v to ", env={"PATH": "/usr/bin:/bin"})  # fmt: skip
    assert never.returncode == 1 and "refused" in never.stderr


def test_local_logs_the_whole_output(tmp_path):
    log = tmp_path / "deploy.log"
    done = local(["printf", "%s", "hello"], log=log)
    assert done.ok and done.stdout == "hello"
    assert log.read_text() == "$ printf %s hello\nhello\n[exit 0]\n"


#: A deploy.sh that never ends, with a child of its own (as its `ssh`), whose PID it records.
HANGS = r"""
sleep 300 &
echo $! > "$1/child"
sleep 300
"""


def _gone(pid: int, within: float = 10) -> bool:
    import os
    import time

    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _child(tmp_path) -> int:
    import time

    for _ in range(100):
        if (tmp_path / "child").exists() and (tmp_path / "child").read_text().strip():
            return int((tmp_path / "child").read_text())
        time.sleep(0.05)
    raise AssertionError("the script never started its child")


def test_a_timeout_stops_the_whole_deploy(tmp_path):
    """Codex round 4 on #363: deploy.sh's children don't outlive its timeout."""
    argv = ["bash", "-c", HANGS, "bash", str(tmp_path)]
    done = typed(argv, answer="v\n", prompt="Type v to ", env={"PATH": "/usr/bin:/bin"}, timeout=1)
    assert done.returncode == 124 and "timed out" in done.stderr
    assert _gone(_child(tmp_path)), "deploy.sh's child survived the timeout"


def test_an_interrupt_stops_the_whole_deploy_before_it_goes_on(tmp_path):
    """Codex round 4 on #363: SIGTERM or SIGHUP raise in run.py; the deploy must not go on."""
    import signal

    class Interrupted(Exception):
        pass

    def interrupt(signum, frame):
        raise Interrupted()

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(Interrupted):
            typed(["bash", "-c", HANGS, "bash", str(tmp_path)], answer="v\n", prompt="Type v to ",
                  env={"PATH": "/usr/bin:/bin"}, timeout=60)  # fmt: skip
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert _gone(_child(tmp_path)), "deploy.sh's child survived the interrupt"
