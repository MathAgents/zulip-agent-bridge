"""Releases from the public repository (#958), offline: `release_repo` is
`MathAgents/zulip-agent-bridge`, whose releases are exported commits, one per release, each on
the previous public release and none on a private commit, their tags signed with the deployer
key. A server first installed from a private commit gets its last hand deploy from the public
one, and its in-band upgrades from the public repository after it.

The rig is deploy.sh's (`test_adminserver_deploy.Rig`): its repository plays the private one, a
bare repository beside it the public one. `export` makes a public release as `tools/publish`
does for the shape that matters here: the private release's tree less what the export drops, one
commit on the previous public release, with its `Exported-from:` trailer, and its tag signed in
the public repository by the same key as the private tag.

- **The hand deploy across the two histories** (BOOTSTRAP §13.4): the server runs v0.3.0, a
  private commit, as production ran v0.2.17; the public repository holds v0.3.1 and v0.3.2 and
  no v0.3.0. The deployer reviews v0.3.2 in the private repository since v0.3.0, then deploys the
  public v0.3.2 from a clone of the public repository into which only the private v0.3.0 tag is
  fetched (the review's base), with `--expect-diff-sha256` of the private review. deploy.sh
  recomputes the diff from the private v0.3.0 to the public v0.3.2 before its prompt: the same
  SHA-256 and release tree is the proof that the two v0.3.2 commits have the same release paths
  byte for byte, which the export guarantees. The bundle the server gets holds the public
  history alone. An export whose release paths differ is refused before the prompt.
- **The in-band upgrade after it:** the public v0.3.3, on the public v0.3.2, fetched from the
  public repository; the running release's tag names the public commit `deploys.jsonl`
  records, and the deployer's review, in either repository, gives the same SHA-256.
"""

import dataclasses
import pathlib
import re
import shutil
import tarfile
import tempfile

import pytest

from workshop.test_adminserver_deploy import (
    DEPLOY,
    FAKE_SSH,
    RELEASE_PATHS,
    ROOT,
    _tools_missing,
    call,
    refused,
    reviewed_sha256,
)
from workshop.test_adminserver_deploy import bootstrapped as bootstrapped  # the fixtures
from workshop.test_adminserver_deploy import index as index
from workshop.test_adminserver_deploy import server as server
from workshop.test_adminserver_upgrade import Upgrade
from zab.admin import upgradehelper as uh

pytestmark = pytest.mark.skipif(bool(_tools_missing()), reason=_tools_missing() or "ok")
#: A file of the private releases that the export leaves out, as it leaves out its own list.
DROPPED = "notes/private.md"
#: What deploy.sh prints for a reviewed diff: its SHA-256 and its release tree.
REVIEWED = re.compile(r"SHA-256 ([0-9a-f]{64}); release tree ([0-9a-f]{64})")
BOOTSTRAP = (ROOT / "docs" / "BOOTSTRAP.md").read_text()


def private_release(rig, version: str, edit=None) -> str:
    """A private release, which holds a file the export drops. Its commit."""

    def change(repo: pathlib.Path) -> None:
        (repo / DROPPED).parent.mkdir(exist_ok=True)
        (repo / DROPPED).write_text(f"{version}: not for the public tree\n")
        if edit:
            edit(repo)

    rig.release(version, edit=change)
    return rig.git("rev-parse", f"{version}^{{commit}}")


def export(
    rig, version: str, public: pathlib.Path, parent: str | None = None, alter=None, drop=()
) -> str:
    """The public release of the private `version`: its tree less DROPPED (and, for a test,
    with `alter(tree)`'s blob changes), one commit on `parent`, the previous public release,
    pushed to `public` and tagged there, signed by the deployer's key. The public commit."""
    private = rig.git("rev-parse", f"{version}^{{commit}}")
    with tempfile.TemporaryDirectory(prefix="export-index-") as tmp:
        env = {**rig.env, "GIT_INDEX_FILE": str(pathlib.Path(tmp) / "index")}

        def git(*args: str, input: str | None = None) -> str:
            argv = ["git", "-C", str(rig.repo), *args]
            done = call(argv, env=env, input=input, capture_output=True, text=True, check=True)
            return done.stdout.strip()

        git("read-tree", private)
        for path in (DROPPED, *drop):  # and, for a test, what the export should have kept
            git("update-index", "--force-remove", path)
        for path, text in (alter or {}).items():
            blob = git("hash-object", "-w", "--stdin", input=text)
            git("update-index", "--cacheinfo", f"100644,{blob},{path}")
        tree = git("write-tree")
        message = f"zab {version}\n\nExported-from: {private}\n"
        commit = git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message)
    # only the new commit and its own history travel: nothing private is reachable from it
    rig.git("push", "-q", str(public), f"{commit}:refs/heads/main")
    key = f"user.signingkey={rig.keys['drew']}"
    tag = ["tag", "-s", "-m", f"zab {version}", version, commit]
    rig.run("git", "-C", public, "-c", "gpg.format=ssh", "-c", key, *tag, check=True)
    return commit


def block(step: str) -> str:
    """The first shell block of *Onto the public repository's commits* after `step`'s text."""
    section = BOOTSTRAP[BOOTSTRAP.index("**Onto the public repository's commits**") :]
    after = section[section.index(step) :]
    start = after.index("```sh\n") + len("```sh\n")
    return "\n".join(line.strip() for line in after[start : after.index("```", start)].splitlines())


def as_documented(rig, public: pathlib.Path, step: str, version: str, private: str):
    """`step`'s block of BOOTSTRAP §13.4 as a deployer runs it: `~/zab` the rig's repository,
    `~/zab-admin/etc` its etc, PUBLIC the public repository, RUNNING the release the server runs,
    V and C the release and its private commit."""
    home = pathlib.Path(rig.env["HOME"])
    for name, target in (("zab", rig.repo), ("zab-admin", rig.etc.parent)):
        if not (home / name).exists():
            (home / name).symlink_to(target)
    text = block(step).replace(
        "PUBLIC=<the public zab repository's clone URL>   # README.md's install line gives it", ""
    )
    assert "<" not in text.replace("2>&1", ""), text  # no placeholder left
    lead = f"set -u\nPUBLIC={public}\nRUNNING=v0.3.0\nV={version}\nC={private}\n"
    return rig.run("bash", "-c", lead + text), home / f"zab-deploy-{version}"


def capture_ssh(rig) -> pathlib.Path:
    """deploy.sh's ssh, which keeps a copy of the payload it copies to the server: the tar."""
    sent = rig.tmp / "sent.tar"
    copy_line = '*"tar --warning=no-unknown-keyword -xf -"*'
    script = FAKE_SSH.replace(
        'exec sh -c "$1"\n',
        f'case $1 in {copy_line}) tee {sent} | sh -c "$1"; exit $? ;; esac\nexec sh -c "$1"\n',
    )
    ssh = rig.tmp / "bin" / "ssh-capture"
    ssh.write_text(script)
    ssh.chmod(0o755)
    rig.env["ZAB_DEPLOY_SSH"] = str(ssh)
    return sent


def public_repo(rig) -> pathlib.Path:
    public = rig.tmp / "public.git"
    rig.run("git", "init", "-q", "--bare", "-b", "main", public, check=True)
    return public


def deploy_from(rig, clone: pathlib.Path, version: str, sha: str):
    where = ["--repo", clone, "--etc", rig.etc, "--out", rig.tmp / "out", "--host", "admin.test"]
    argv = [*where, "--expect-diff-sha256", sha, version]
    return rig.run("bash", DEPLOY, *argv, input=f"{version}\n")


def test_the_last_hand_deploy_takes_the_public_commit_reviewed_in_private(server, tmp_path):
    rig = server
    private = {"v0.3.0": rig.git("rev-parse", "v0.3.0^{commit}")}
    private["v0.3.1"] = private_release(rig, "v0.3.1")  # the first public release, never deployed
    private["v0.3.2"] = private_release(
        rig, "v0.3.2", edit=lambda repo: (repo / "workshop/adminserver/sysusers.conf").write_text(
            "# changed in v0.3.2\n"
        )
    )  # fmt: skip
    public = public_repo(rig)
    p1 = export(rig, "v0.3.1", public)
    p2 = export(rig, "v0.3.2", public, parent=p1)
    # the private review, with the running release's deploy.sh, since it
    commit = private["v0.3.2"]
    where = ["--repo", rig.repo, "--etc", rig.etc, "--out", rig.tmp / "out", "--review", commit]
    review = rig.run("bash", DEPLOY, *where, "--since", "v0.3.0")
    assert review.returncode == 0, review.stderr
    sha, tree = REVIEWED.search(review.stderr).groups()
    made, clone = as_documented(rig, public, "2. **The deploy clone,**", "v0.3.2", commit)
    assert made.returncode == 0, made.stderr
    fingerprint = rig.run(
        "ssh-keygen", "-lf", f"{rig.keys['drew']}.pub", check=True
    ).stdout.split()[1]
    good = f'Good "git" signature for drew@example.org with ED25519 key {fingerprint}'
    assert made.stdout.splitlines() == [good, good]  # the published V's, the running release's
    tied, _ = as_documented(rig, public, "3. **The published `V` is `C`", "v0.3.2", commit)
    assert tied.stdout.splitlines() == [
        f"Exported-from: {commit}",
        commit,
        "only removals",
        "nothing installed removed",
    ], tied.stdout + tied.stderr
    sent = capture_ssh(rig)
    result = deploy_from(rig, clone, "v0.3.2", sha)
    assert result.returncode == 0, result.stderr
    # the public commit's diff from the private base is the one reviewed, and its release tree
    assert "It is the diff you reviewed (--expect-diff-sha256)" in result.stderr
    assert REVIEWED.search(result.stderr).groups() == (sha, tree)
    assert "this deploy.sh is not v0.3.2's" not in result.stderr  # the public one, byte for byte
    record = rig.records()[-1]
    assert (record["event"], record["version"], record["commit"]) == ("upgrade", "v0.3.2", p2)
    assert (record["base"], record["diff_sha256"]) == ("v0.3.0", sha)
    assert rig.current().endswith("v0.3.2")
    # the bundle the server got: the public tag and the public history alone, complete, with no
    # prerequisite, so no private commit object, not even the base's
    with tarfile.open(sent) as tar:
        member = tar.extractfile("./release.bundle")
        assert member is not None
        bundle = tmp_path / "release.bundle"
        bundle.write_bytes(member.read())
    heads = rig.run("git", "bundle", "list-heads", bundle, check=True).stdout.split()
    tag = rig.run("git", "-C", public, "rev-parse", "refs/tags/v0.3.2", check=True).stdout.strip()
    assert heads == [tag, "refs/tags/v0.3.2"]
    fresh = tmp_path / "fresh"
    rig.run("git", "init", "-q", fresh, check=True)
    verified = rig.run("git", "-C", fresh, "bundle", "verify", bundle)
    assert verified.returncode == 0, verified.stderr
    assert "The bundle records a complete history." in verified.stdout + verified.stderr
    get = ["fetch", "-q", bundle, "refs/tags/v0.3.2:refs/tags/v0.3.2"]
    rig.run("git", "-C", fresh, *get, check=True)
    commits = rig.run("git", "-C", fresh, "rev-list", "--all", check=True).stdout.split()
    assert sorted(commits) == sorted([p1, p2])
    every_private = rig.git("rev-list", "--all").split()
    assert len(every_private) == 4  # the rig's first commit and the three releases
    for c in every_private:
        assert rig.run("git", "-C", fresh, "cat-file", "-e", c).returncode != 0, c
    assert "refs/private/" not in rig.run("git", "bundle", "list-heads", bundle).stdout
    blobs = rig.run("git", "-C", fresh, "ls-tree", "-r", "--name-only", p2, check=True).stdout
    assert DROPPED not in blobs.split()  # the export's drop, in every public commit

    # The next release, in-band, from the public repository: v0.3.3 on the public v0.3.2.
    private["v0.3.3"] = private_release(
        rig, "v0.3.3", edit=lambda repo: (repo / "workshop/adminserver/sysusers.conf").write_text(
            "# changed in v0.3.3\n"
        )
    )  # fmt: skip
    p3 = export(rig, "v0.3.3", public, parent=p2)
    in_private = reviewed_sha256(rig, "v0.3.3", since="v0.3.2")
    rig.run("git", "-C", clone, "fetch", "-q", public, "refs/tags/*:refs/tags/*", check=True)
    where = ["--repo", clone, "--etc", rig.etc, "--out", rig.tmp / "out", "--review", p3]
    in_public = rig.run("bash", DEPLOY, *where, "--since", "v0.3.2")
    assert in_public.returncode == 0, in_public.stderr
    assert REVIEWED.search(in_public.stderr).group(1) == in_private  # either repository's
    up = Upgrade(rig, tmp_path)
    up.paths = dataclasses.replace(up.paths, fetch_url=str(public))
    plan = up.ask(uh.PLAN, release="v0.3.3", sha256=in_private)
    assert plan["ok"] is True, plan["message"]
    assert (plan["running"], plan["base_commit"], plan["commit"]) == ("v0.3.2", p2, p3)
    found = up.ask(uh.APPLY, release="v0.3.3", sha256=in_private, commit=p3)
    assert found["ok"] is True and found["current"] == "v0.3.3", found
    assert rig.records()[-1]["commit"] == p3


def test_an_export_whose_release_paths_differ_is_refused_before_the_prompt(server):
    """The private review's SHA-256 holds the deploy to the release paths reviewed: a public
    commit with one release path changed is refused, and the server keeps what it runs."""
    rig = server
    private_release(rig, "v0.3.1")
    public = public_repo(rig)
    p1 = export(rig, "v0.3.1", public, alter={"workshop/adminserver/sysusers.conf": "# other\n"})
    sha = reviewed_sha256(rig, "v0.3.1", since="v0.3.0")
    private = rig.git("rev-parse", "v0.3.1^{commit}")
    _, clone = as_documented(rig, public, "2. **The deploy clone,**", "v0.3.1", private)
    result = deploy_from(rig, clone, "v0.3.1", sha)
    refused(result, f"this deploy's diff has SHA-256 [0-9a-f]{{64}}, not {sha}, the one you rev")
    assert "Type v0.3.1" not in result.stderr
    assert rig.current().endswith("v0.3.0") and [r["event"] for r in rig.records()] == ["bootstrap"]
    assert rig.run("git", "-C", clone, "cat-file", "-e", p1).returncode == 0  # it was the public


def test_without_the_base_tag_the_deploy_clone_is_refused(server):
    """The public repository alone has no tag of the release the server runs: deploy.sh refuses
    to diff from nothing, so the private base tag must be fetched in (BOOTSTRAP §13.4)."""
    rig = server
    private_release(rig, "v0.3.1")
    public = public_repo(rig)
    export(rig, "v0.3.1", public)
    clone = rig.tmp / "public-only"
    rig.run("git", "clone", "-q", public, clone, check=True)
    result = deploy_from(rig, clone, "v0.3.1", "0" * 64)
    refused(result, r"v0\.3\.0: no such tag")
    assert rig.current().endswith("v0.3.0")


def test_an_export_whose_other_files_differ_is_caught_before_the_deploy(server):
    """#961's review, B3: the review's SHA-256 covers the release paths alone, not the rest of
    `src/zab`, `README.md`, `LICENSE` and `.gitignore`, from which `--with-wheel` builds the fleet
    wheel. A public commit that differs there passes the SHA-256, so BOOTSTRAP §13.4's step 3
    ties the published V to the reviewed C before the deploy: C named by its trailer, and its
    tree C's with files removed, none installed. Here it names the changed file and stops."""
    rig = server
    private = private_release(rig, "v0.3.1")
    public = public_repo(rig)
    p1 = export(rig, "v0.3.1", public, alter={"src/zab/bridge.py": "print('not reviewed')\n"})
    sha = reviewed_sha256(rig, "v0.3.1", since="v0.3.0")
    made, clone = as_documented(rig, public, "2. **The deploy clone,**", "v0.3.1", private)
    assert made.returncode == 0, made.stderr
    # the release paths are the same, so the SHA-256 alone would let it through
    where = ["--repo", clone, "--etc", rig.etc, "--out", rig.tmp / "out", "--review", p1]
    review = rig.run("bash", DEPLOY, *where, "--since", "v0.3.0")
    assert REVIEWED.search(review.stderr).group(1) == sha
    tied, _ = as_documented(rig, public, "3. **The published `V` is `C`", "v0.3.1", private)
    assert tied.stdout.splitlines() == [
        f"Exported-from: {private}",
        private,
        "M\tsrc/zab/bridge.py",
        "nothing installed removed",
    ], tied.stdout + tied.stderr
    assert "only removals" not in tied.stdout


def test_an_export_of_another_commit_or_without_an_installed_file_is_caught(server):
    """Step 3's two other lines: the trailer names the commit reviewed, and no file the server
    installs or builds from is removed."""
    rig = server
    private = private_release(rig, "v0.3.1")
    other = rig.git("rev-parse", "v0.3.0^{commit}")
    public = public_repo(rig)
    export(rig, "v0.3.1", public, drop=("README.md", "src/zab/bridge.py"))  # the wheel takes both
    as_documented(rig, public, "2. **The deploy clone,**", "v0.3.1", private)
    tied, _ = as_documented(rig, public, "3. **The published `V` is `C`", "v0.3.1", private)
    assert tied.stdout.splitlines() == [
        f"Exported-from: {private}",
        private,
        "only removals",
        "README.md",
        "src/zab/bridge.py",
    ], tied.stdout + tied.stderr
    assert "nothing installed removed" not in tied.stdout
    # reviewed as another commit than the one exported: the trailer and the hash differ
    shutil.rmtree(pathlib.Path(rig.env["HOME"]) / "zab-deploy-v0.3.1")
    as_documented(rig, public, "2. **The deploy clone,**", "v0.3.1", other)
    tied, _ = as_documented(rig, public, "3. **The published `V` is `C`", "v0.3.1", other)
    assert tied.stdout.splitlines()[:2] == [f"Exported-from: {private}", other]


STEP3 = "3. **The published `V` is `C`"


def test_a_removed_file_git_would_quote_is_still_seen(server):
    r"""#961's second review, N1: git quotes a name with non-ASCII bytes, `"` or `\` (its
    `core.quotePath`), which would hide it from a match anchored at the name's start. Step 3 runs
    git with `core.quotePath=false` and lets a quote stand before the prefix: both removals show."""
    rig = server

    def odd(repo: pathlib.Path) -> None:
        (repo / "src/zab/café.py").write_text("x = 1\n")
        (repo / 'src/zab/a"b.py').write_text("x = 2\n")

    private = private_release(rig, "v0.3.1", edit=odd)
    public = public_repo(rig)
    export(rig, "v0.3.1", public, drop=("src/zab/café.py", 'src/zab/a"b.py'))
    as_documented(rig, public, "2. **The deploy clone,**", "v0.3.1", private)
    tied, _ = as_documented(rig, public, STEP3, "v0.3.1", private)
    lines = tied.stdout.splitlines()
    assert lines[:3] == [f"Exported-from: {private}", private, "only removals"], tied.stdout
    assert sorted(lines[3:]) == sorted(['"src/zab/a\\"b.py"', "src/zab/café.py"]), lines
    assert "nothing installed removed" not in tied.stdout


def test_a_failed_diff_prints_no_success_line(server):
    """#961's second review, N2: without `C` in the deploy clone (step 2's fetch failed or was
    skipped), each git command of step 3 fails, and neither success line is printed."""
    rig = server
    private = private_release(rig, "v0.3.1")
    public = public_repo(rig)
    export(rig, "v0.3.1", public)
    _, clone = as_documented(rig, public, "2. **The deploy clone,**", "v0.3.1", private)
    rig.run("git", "-C", clone, "update-ref", "-d", "refs/private/C", check=True)
    tied, _ = as_documented(rig, public, STEP3, "v0.3.1", private)
    assert tied.stdout.splitlines() == [f"Exported-from: {private}"], tied.stdout
    assert "only removals" not in tied.stdout and "nothing installed removed" not in tied.stdout
    assert "refs/private/C" in tied.stderr  # git's own errors name what is missing


def test_step_3_checks_every_file_the_server_installs_or_builds_from():
    """#961's second review, N3: step 3's list of what no removal may touch covers deploy.sh's
    wheel tree, in both its builds (the laptop's and the server's), and every release path, so
    that a new input of either can't go unchecked."""
    pattern = re.search(r"grep -E '\^\"\?(\([^']+\))'", block(STEP3)).group(1)
    installed = re.compile(f"^{pattern}")
    deploy = DEPLOY.read_text()
    trees = re.findall(r"local -a (?:pass=\(\) )?tree=\(([^)]*)\)", deploy)
    extras = re.findall(r"for f in ([^;]+); do ! g cat-file -e \"\$COMMIT:\$f\"", deploy)
    assert len(trees) == 2 and len(extras) == 2, (trees, extras)  # build_wheel, server_wheel
    wheel = {path for words in trees + extras for path in words.split()}
    assert {"src/zab", "pyproject.toml", "README.md", "LICENSE", ".gitignore"} <= wheel
    for path in sorted(wheel | set(RELEASE_PATHS)):
        assert installed.match(path), f"{path}: not in step 3's list of installed files"
