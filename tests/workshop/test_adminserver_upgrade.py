"""The root helpers of `!upgrade-server` (`zab.admin.upgradehelper`; docs/ADMIN.md §1.6, #569),
offline.

Each test makes a release repository with deploy.sh's test rig (`test_adminserver_deploy.Rig`:
throwaway SSH keys that sign its tags, a local index), and a server's state in a temporary
directory: `current` names v0.3.0, whose deploy.sh is this tree's. The helper fetches from the
rig's repository over git's `file` transport (`Paths.fetch_url`, the tests' seam), in place of
`release_repo` over SSH, and runs unprivileged: the requests' owner is the test's own uid.
"""

import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from workshop.test_adminserver_deploy import (
    DEPLOY,
    UNIT_STEPS,
    WHEELS,
    Rig,
    _rooted,
    _tools_missing,
    _user_manager,
    call,
    reviewed_sha256,
    systemctl_log,
)
from workshop.test_adminserver_deploy import bootstrapped as bootstrapped  # the fixtures
from workshop.test_adminserver_deploy import index as index
from zab.admin import upgradehelper as uh

pytestmark = pytest.mark.skipif(bool(_tools_missing()), reason=_tools_missing() or "ok")
SERVER = DEPLOY.parent
REPO = "git@github.com:Example/zab.git"
NONCE = "0123456789abcdef0123456789abcdef"


class Server:
    """The helper's view of a server: /etc/zab-admin (the rig's etc), /opt/zab-admin with
    `current` at v0.3.0, and its own state; the rig's repository as release_repo."""

    def __init__(self, tmp: pathlib.Path, rig: Rig) -> None:
        self.rig = rig
        self.state, self.opt = tmp / "state", tmp / "opt"
        (self.state / "requests").mkdir(parents=True, mode=0o700)
        (self.state / "results").mkdir(mode=0o755)
        text = (rig.etc / "admin.toml").read_text()
        (rig.etc / "admin.toml").write_text(
            text.replace("console_url", f'release_repo = "{REPO}"\nconsole_url', 1)
        )
        self.run_release("v0.3.0")
        self.paths = uh.Paths(
            state=str(self.state), etc=str(rig.etc), key=str(tmp / "release-key"),
            opt=str(self.opt), fetch_url=str(rig.repo),
        )  # fmt: skip

    def run_release(self, version: str) -> None:
        """`current` → releases/<version>, which holds this tree's server files, and deploy.sh's
        record of its install, at its tag's commit."""
        release = self.opt / "releases" / version
        shutil.copytree(SERVER, release / "workshop" / "adminserver")
        current = self.opt / "current"
        event = "upgrade" if current.is_symlink() else "bootstrap"
        if current.is_symlink():
            current.unlink()
        current.symlink_to(release)
        self.running = version
        commit = self.rig.git("rev-parse", f"{version}^{{commit}}")
        record = {"ts": "2026-11-06T00:00:00Z", "event": event, "version": version}
        with (self.opt / "deploys.jsonl").open("a") as f:
            f.write(json.dumps({**record, "commit": commit, "tree": "0" * 64}) + "\n")

    def sha256(self, release: str) -> str:
        """The deployer's own review's SHA-256 of `release` since the running release, as
        `deploy.sh --review` prints it; zeros for a tag it can't review (one the plan refuses
        before its review)."""
        try:
            return reviewed_sha256(self.rig, release, since=self.running)
        except (AssertionError, subprocess.CalledProcessError):
            return "0" * 64

    def request(self, action: str = uh.PLAN, **fields) -> pathlib.Path:
        body = {"version": 1, "plan": "P-7", "nonce": NONCE, "release": "v0.3.1",
                "sha256": "0" * 64, **fields}  # fmt: skip
        path = self.state / "requests" / f"{action}.json"
        path.write_text(json.dumps(body))
        return path

    def plan(self, **fields) -> dict:
        """A plan request, by default with the SHA-256 the deployer's review printed."""
        fields.setdefault("sha256", self.sha256(fields.get("release", "v0.3.1")))
        self.request(**fields)
        code = uh.run(uh.PLAN, self.paths, os.getuid())
        name = fields.get("plan", "P-7")
        found = json.loads((self.state / "results" / f"{name}.plan.json").read_text())
        assert code == (0 if found["ok"] else 1)
        assert not (self.state / "requests" / "plan.json").exists()  # taken, whatever it was
        return found


@pytest.fixture
def server(tmp_path, index):
    (tmp_path / "rig").mkdir()
    rig = Rig(tmp_path / "rig", index)
    rig.release("v0.3.0")
    return Server(tmp_path, rig)


def refused(found: dict, pattern: str) -> None:
    assert found["ok"] is False, found
    assert re.search(pattern, found["message"]), found["message"]


# --- a good plan ------------------------------------------------------------------------------


def test_a_signed_release_is_planned_with_the_reviews_sha256(server):
    """The plan names both tags, by object, commit, signer and key; the SHA-256 that the
    deployer's own `deploy.sh --review … --since v0.3.0` printed, which the request carried,
    with the release tree; then the review, without its unprivileged rest, and the CHANGELOG
    section."""
    rig = server.rig
    rig.release(
        "v0.3.1",
        edit=lambda repo: (
            (repo / "workshop/adminserver/sysusers.conf").write_text("# changed\n"),
            (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.3.1 — 2026-11-07\n\nNew.\n\n"
                                               "## 0.3.0 — 2026-11-01\n\nOld.\n"),
        ),
    )  # fmt: skip
    sha = server.sha256("v0.3.1")
    found = server.plan(sha256=sha)
    assert found["ok"] is True, found["message"]
    commit = rig.git("rev-parse", "v0.3.1^{commit}")
    base = rig.git("rev-parse", "v0.3.0^{commit}")
    assert (found["release"], found["commit"], found["running"]) == ("v0.3.1", commit, "v0.3.0")
    assert (found["base_commit"], found["signer"]) == (base, "drew@example.org")
    assert (found["plan"], found["nonce"]) == ("P-7", NONCE) and "downgrade" not in found
    assert found["diff_sha256"] == sha
    text = found["text"]
    lines = text.splitlines()
    fingerprint = rig.run("ssh-keygen", "-lf", f"{rig.keys['drew']}.pub").stdout.split()[1]
    signed = f"signed by drew@example.org with ED25519 key {fingerprint}"
    tags = {v: rig.git("rev-parse", f"refs/tags/{v}") for v in ("v0.3.0", "v0.3.1")}
    assert lines[:3] == [
        "!upgrade-server v0.3.1, from v0.3.0: newer than v0.3.0",
        f"release v0.3.1: tag {tags['v0.3.1']}, commit {commit}, {signed}",
        f"base, running v0.3.0: tag {tags['v0.3.0']}, commit {base}, {signed}",
    ]
    assert lines[3] == (
        f"the reviewed diff, v0.3.0..{commit[:12]}, SHA-256 {sha}; release tree "
        f"{found['tree_sha256']} ({len(rig_paths(rig))} paths)"
    )
    assert lines[4].startswith("  the sha256= you gave: the same")
    assert "privileged files changed: 2" in text  # sysusers.conf, pyproject.toml's version
    assert "  P M workshop/adminserver/sysusers.conf" in lines
    assert "render_policy.py: unchanged since v0.3.0" in lines
    assert "+# changed" in text
    assert "==== The rest (" not in text
    assert text.endswith("==== CHANGELOG.md, 0.3.1 ====\n## 0.3.1 — 2026-11-07\n\nNew.\n\n")


def rig_paths(rig) -> list[str]:
    text = (rig.repo / "workshop/adminserver/release-paths").read_text()
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def test_another_sha256_than_the_reviews_is_refused(server):
    """The design's addendum (#569): a deployer's request installs only the diff they reviewed,
    from the base they reviewed it from. Another SHA-256 (a review from another base, of another
    commit, or a typo) refuses the plan before it is offered."""
    rig = server.rig
    rig.release("v0.3.1")
    sha = server.sha256("v0.3.1")
    found = server.plan(sha256="1" * 64)
    refused(found, rf"^the reviewed diff v0.3.0\.\.[0-9a-f]{{12}} has SHA-256 {sha}, not the sha2")
    assert (
        f"not the sha256= you gave, {'1' * 64}: review it with v0.3.0's own deploy.sh"
        in (found["message"])
    )  # review of #893, N6, N1: both values, and which script and base give the right one
    assert "nothing was planned" in found["message"] and "text" not in found
    other = reviewed_sha256(rig, "v0.3.1")  # from the empty tree: another base
    refused(server.plan(sha256=other), "not the sha256= you gave")
    job = uh.start(server.paths, "x")  # review of #889, N3: never adopted, unreviewed
    with pytest.raises(uh.Refused):
        uh.git(job, "rev-parse", "--verify", "--quiet", "refs/tags/v0.3.1")


#: A stand-in for the running release's deploy.sh (review of #893, B3): `--review`'s outputs,
#: forged: a diff `diff\n` in each of `$DIFFS`, whose SHA-256 it prints as `$FORGE_SHA` (or
#: none), and `$REVIEW` (printf's format) as the review, into its `--out`.
FAKE_REVIEW = """#!/bin/bash
for out; do :; done  # the last argument: --out's directory
for d in $DIFFS; do printf 'diff\\n' > "$out/$d.diff"; done
printf '$REVIEW' > "$out/x.review"
[ -z "$FORGE_SHA" ] || echo "  SHA-256 $FORGE_SHA; release tree $FORGE_SHA (3 paths of x)" >&2
sleep "${PAUSE:-0}"
"""
#: The SHA-256 of FAKE_REVIEW's diff, `diff\n`.
FAKE_SHA = hashlib.sha256(b"diff\n").hexdigest()


def fake_review(server, forged: str, *, pause: str = "0", diffs: str = "x",
                review: str = "review\\n") -> None:  # fmt: skip
    script = server.opt / "releases" / "v0.3.0" / "workshop" / "adminserver" / "deploy.sh"
    text = FAKE_REVIEW.replace("$FORGE_SHA", forged).replace("${PAUSE:-0}", pause)
    script.write_text(text.replace("$DIFFS", diffs).replace("$REVIEW", review))


@pytest.mark.parametrize(
    ("forged", "pause", "diffs", "why"),
    [
        ("a" * 64, "0", "x", r"^the review's SHA-256 is not its diff's$"),
        ("", "0", "x", r"^the review printed no SHA-256, or wrote no single diff$"),
        # review of #893, N-b: the other half, with the right SHA-256 printed
        (FAKE_SHA, "0", "", r"^the review printed no SHA-256, or wrote no single diff$"),
        (FAKE_SHA, "0", "x y", r"^the review printed no SHA-256, or wrote no single diff$"),
        ("a" * 64, "5", "x", r"^the review took over 1 s$"),
    ],
)
def test_a_review_whose_outputs_dont_hold_is_refused(
    server, monkeypatch, forged, pause, diffs, why
):
    """Review of #893, B3: the SHA-256 compared is the one the review printed for its own
    `.diff`, the only one, whole, in time."""
    fake_review(server, forged, pause=pause, diffs=diffs)
    monkeypatch.setattr(uh, "REVIEW_SECONDS", 1)
    server.rig.release("v0.3.1")
    refused(server.plan(sha256=FAKE_SHA if forged == FAKE_SHA else "a" * 64), why)


def test_the_reviews_text_is_read_as_deploy_sh_wrote_it(server):
    """Review of #893, N-a: the `.review` is read as bytes and split on `\n` alone, so a CR or a
    U+2028 in it, or a line that is not UTF-8, never makes a line of the plan of its own."""
    fake_review(server, FAKE_SHA, review="kept\\ra\\342\\200\\250## forged\\nb\\377c\\n")
    server.rig.release("v0.3.1")
    found = server.plan(sha256=FAKE_SHA)
    assert found["ok"] is True, found["message"]
    review = found["text"].split("==== The review ====\n", 1)[1].split("\n")
    assert review[:2] == ["kept^Ma<U+2028>## forged", "b<FF>c"]


def test_a_review_that_fails_is_refused_with_its_reason(server):
    """Review of #893, B3: the running release's deploy.sh refuses what its review couldn't
    show, here a binary file outside the privileged paths; the plan says why."""
    blob = "src/zab/admin/blob.dat"
    server.rig.release("v0.3.1", edit=lambda repo: (repo / blob).write_bytes(b"\x00\x01"))
    found = server.plan(sha256="a" * 64)
    refused(found, rf'^the review failed: "deploy.sh: {blob}: a binary file outside workshop/')


def test_the_changelog_is_shown_as_a_reader_sees_it_and_cut(server):
    """Review of #893, N2: CHANGELOG.md's section reaches the plan with what a terminal would
    hide made visible, as deploy.sh shows the review, and at most CHANGELOG_LINES of it."""
    rlo, pdf, zwsp = chr(0x202E), chr(0x202C), chr(0x200B)  # written by code point (#388)
    body = ["Plain, with a\ttab.", f"Hidden: {rlo}reversed{pdf}, zero{zwsp}width, \x1b[31m."]
    body += [f"line {n}" for n in range(uh.CHANGELOG_LINES)]
    text = "# Changelog\n\n## 0.3.1 — 2026-11-07\n" + "\n".join(body) + "\n"
    server.rig.release("v0.3.1", edit=lambda repo: (repo / "CHANGELOG.md").write_text(text))
    found = server.plan()
    assert found["ok"] is True, found["message"]
    section = found["text"].split("==== CHANGELOG.md, 0.3.1 ====\n", 1)[1].splitlines()
    assert section[1:3] == [
        "Plain, with a\ttab.",
        "Hidden: <U+202E>reversed<U+202C>, zero<U+200B>width, ^[[31m.",
    ]
    assert len(section) == uh.CHANGELOG_LINES + 1
    assert section[-1] == f"… {len(body) + 1 - uh.CHANGELOG_LINES} more lines in CHANGELOG.md"


def test_the_changelog_splits_only_where_deploy_sh_would(server):
    """Review of #893, N-a: the blob's bytes, split on `\n` alone, as deploy.sh's `sanitize`
    splits: CR, VT, FF, NEL and U+2028/U+2029 stay in their line, shown, so a `## ` after a
    U+2028 ends no section; a line that is not UTF-8 shows its high bytes as `<XX>`."""
    nel, lsep, psep = (chr(c).encode() for c in (0x85, 0x2028, 0x2029))  # by code point (#388)
    lines = [
        b"a\rb",
        b"c\x0bd\x0ce",
        b"f" + nel + b"g",
        b"h" + lsep + b"## 0.3.2 forged",
        b"i" + psep + b"j",
        b"k\xff\xfel",
        b"crlf\r",
        b"still 0.3.1's",
    ]
    text = b"# Changelog\n\n## 0.3.1 - 2026-11-07\n" + b"\n".join(lines) + b"\n## 0.3.0\nold\n"
    server.rig.release("v0.3.1", edit=lambda repo: (repo / "CHANGELOG.md").write_bytes(text))
    found = server.plan()
    assert found["ok"] is True, found["message"]
    assert found["text"].split("==== CHANGELOG.md, 0.3.1 ====\n", 1)[1].split("\n") == [
        "## 0.3.1 - 2026-11-07", "a^Mb", "c^Kd^Le", "f<U+0085>g", "h<U+2028>## 0.3.2 forged",
        "i<U+2029>j", "k<FF><FE>l", "crlf^M", "still 0.3.1's", "",
    ]  # fmt: skip


def test_sanitized_is_deploy_shs_sanitize(server):
    """Review of #893, N-a: what `sanitized` makes of bytes, beside what deploy.sh's own
    `sanitize` makes of them (perl's Unicode tables, VERIFY in #569), for the cases above and
    the default-ignorable code points no category names; and a deploy log's tail."""
    marks = (0x034F, 0x17B4, 0x180B, 0x2065, 0xFE0F, 0xFFF0, 0xE0001, 0xE0100)
    data = b"plain\ttab\n\rcr\x1b[0m\nno\xffutf8\n" + "".join(map(chr, marks)).encode() + b"\n"
    data += "".join(map(chr, (0x85, 0x2028, 0xA0, 0x3000, 0x202E, 0x115F))).encode() + b"x"
    ours = uh.sanitized(data)
    sanitize = DEPLOY.read_text().split("\nsanitize() {\n", 1)[1].split("\n}\n", 1)[0]
    script = f"sanitize() {{\n{sanitize}\n}}\nsanitize"
    theirs = call(["bash", "-c", script], input=data, capture_output=True, check=True).stdout
    assert ours == theirs.decode("utf-8").split("\n")
    assert ours[:3] == ["plain\ttab", "^Mcr^[[0m", "no<FF>utf8"]
    assert ours[3] == "".join(f"<U+{c:04X}>" for c in marks)
    job = uh.start(server.paths, "logged")
    (pathlib.Path(job.work) / uh.DEPLOY_LOG).write_bytes(data)
    assert uh.logged(job, uh.DEPLOY_LOG) == ours[-uh.TAIL :]


def test_a_changed_render_policy_is_said(server):
    """§2.9: the server never renders the policy for the account owner; the plan says when the
    script changed, for the deployer to render it on their own machine."""
    policy = "workshop/adminserver/render_policy.py"
    server.rig.release("v0.3.1", edit=lambda repo: (repo / policy).write_text(
        (repo / policy).read_text() + "# changed\n"))  # fmt: skip
    found = server.plan()
    assert "render_policy.py: CHANGED since v0.3.0: render the role's policy" in found["text"]


def test_a_release_candidate_has_the_unreleased_section(server):
    rig = server.rig
    text = (rig.etc / "admin.toml").read_text()
    (rig.etc / "admin.toml").write_text(text.replace('release = "final"', 'release = "rc"'))
    rig.release("v0.3.1rc1", edit=lambda repo: (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\nSoon.\n"))  # fmt: skip
    found = server.plan(release="v0.3.1rc1")
    assert found["ok"] is True, found["message"]
    assert found["text"].endswith("==== CHANGELOG.md, 0.3.1rc1 ====\n## Unreleased\n\nSoon.\n")


def test_no_downgrade_is_planned_in_band(server):
    """An in-band upgrade only goes forward: an older release is refused, and a request that
    asks for a downgrade at all, whatever it says, is refused before anything is read."""
    rig = server.rig
    rig.release("v0.3.1")
    server.run_release("v0.3.1")
    forward = "an in-band upgrade only goes forward: go back with deploy.sh --downgrade"
    refused(server.plan(release="v0.3.0"), rf"^v0.3.0 is older than v0.3.1, which runs: {forward}")
    for asked in (True, False, "yes"):
        refused(server.plan(release="v0.3.0", downgrade=asked), rf"^{forward} from a deployer's")
    assert not (server.state / "repo.git").exists()  # refused before any fetch


def test_an_apply_never_downgrades(server):
    """An apply is refused a downgrade as a plan is (no in-band downgrades): whatever its plan
    on record says, a request naming `downgrade` is refused before anything is read."""
    server.request(uh.APPLY, sha256="0" * 64, commit="0" * 40, downgrade=False)
    assert uh.run(uh.APPLY, server.paths, os.getuid()) == 1
    found = json.loads((server.state / "results" / "P-7.apply.json").read_text())
    refused(found, r"^an in-band upgrade only goes forward: go back with deploy.sh --downgrade")
    assert not (server.state / "work" / "P-7.apply").exists()  # refused before its run began


# --- the checks: deploy.sh's, again -------------------------------------------------------------


def test_an_unsigned_tag_is_refused(server):
    server.rig.release("v0.3.1", signer=None)
    refused(server.plan(), r"^v0.3.1: not signed with an SSH key$")


def test_a_lightweight_tag_is_refused(server):
    server.rig.release("v0.3.1", signer="light")
    refused(server.plan(), r"^v0.3.1: a lightweight tag")


def test_a_tag_of_another_signer_is_refused(server):
    server.rig.release("v0.3.1", signer="mallory")
    refused(server.plan(), r"^v0.3.1: not signed by a key in .*allowed_signers")


def test_a_ref_named_otherwise_than_its_tag_object_is_refused(server):
    """§1.6, check 2: `verify-tag` checks only the object's signature; a ref `v0.3.2` pushed to
    point at the signed `v0.3.1` would verify, and the plan say v0.3.2."""
    rig = server.rig
    rig.release("v0.3.1")
    rig.git("update-ref", "refs/tags/v0.3.2", rig.git("rev-parse", "refs/tags/v0.3.1"))
    refused(server.plan(release="v0.3.2"), r"^refs/tags/v0.3.2 holds a tag object named otherwise")


def test_a_release_candidate_is_refused_under_the_final_pattern(server):
    server.rig.release("v0.3.1rc1")
    refused(
        server.plan(release="v0.3.1rc1"),
        r'a release candidate, and admin.toml has release = "final"',
    )


def test_a_tag_whose_pyproject_names_another_version_is_refused(server):
    rig = server.rig
    rig.release("v0.3.1")
    rig.git("tag", "-d", "v0.3.1")
    rig.sign("v0.3.2")  # the same commit, which says 0.3.1
    refused(server.plan(release="v0.3.2"), r"^v0.3.2: pyproject.toml has another version$")


def test_a_pyproject_read_as_bytes_as_deploy_sh_reads_it(server):
    """Review of #894, B1's sweep: `pyproject.toml`'s version line is read as its bytes, as
    deploy.sh reads it, so a line that ends in a carriage return is not the version's, here as
    there (a text mode would read it as one)."""

    def crlf(repo: pathlib.Path) -> None:
        path = repo / "pyproject.toml"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    server.rig.release("v0.3.1", edit=crlf)
    refused(server.plan(), r"^v0.3.1: pyproject.toml has another version$")


def test_the_running_release_is_not_planned_again(server):
    refused(server.plan(release="v0.3.0"), r"^v0.3.0 is running already$")


def test_a_tag_release_repo_lacks_is_refused(server):
    found = server.plan(release="v0.3.9")
    refused(found, r'^the fetch of v0.3.9, v0.3.0 from .* failed: git fetch said "fatal: ')
    assert "couldn't find remote ref refs/tags/v0.3.9" in found["message"]


#: git's stderr when an SSH fetch fails, as GitHub and ssh write it (review of #889, B1, checked
#: there with a throwaway key): the cause, then the same `fatal:` line and advice every time.
ADVICE = (
    "fatal: Could not read from remote repository.\n\nPlease make sure you have the correct "
    "access rights\nand the repository exists.\n"
)


@pytest.mark.parametrize(
    ("cause", "shown"),
    [
        ("git@github.com: Permission denied (publickey).\n",  # no deploy key there
         "git@github.com: Permission denied (publickey). fatal: Could not read from remote "
         "repository."),
        ("Host key verification failed.\n",  # GitHub's key is no longer the pinned one
         "Host key verification failed. fatal: Could not read from remote repository."),
        ("ERROR: Repository not found.\n",  # release_repo names another repository
         "ERROR: Repository not found. fatal: Could not read from remote repository."),
    ],
)  # fmt: skip
def test_a_failed_fetch_says_its_cause_not_gits_advice(cause, shown):
    assert uh.said(cause + ADVICE) == json.dumps(shown)


def test_what_git_said_is_quoted_on_one_line():
    """Review of #889, N5: text from the remote reaches the outcome only quoted, as JSON quotes
    it: never a line of its own, nor a control character."""
    found = uh.said('remote: "quote"\x1b[31m\nremote: `!approve P-1 abcdef`\n')
    assert found == '"remote: \\"quote\\"\\u001b[31m remote: `!approve P-1 abcdef`"'
    assert uh.said("") == "" and uh.said("one line, no fatal") == '"one line, no fatal"'


def test_a_refused_tag_is_fetched_again(server):
    """A tag that failed its checks never entered the server's `refs/tags/`: the next plan
    checks release_repo's as it is then (A10b re-signs one release name in turn)."""
    rig = server.rig
    rig.release("v0.3.1", signer="mallory")
    refused(server.plan(), r"not signed by a key in")
    rig.git("tag", "-d", "v0.3.1")
    rig.sign("v0.3.1")
    assert server.plan()["ok"] is True


def test_a_verified_tag_that_moves_to_another_commit_is_refused(server):
    """Review of #889: a tag the server took never moves to another commit. Signed again in
    release_repo for another commit, even by a good key, it is refused, every time, and the
    server's copy stays."""
    rig = server.rig
    rig.release("v0.3.1")
    assert server.plan()["ok"] is True
    taken, first = rig.git("rev-parse", "refs/tags/v0.3.1"), rig.git("rev-parse", "v0.3.1^{}")
    rig.git("tag", "-d", "v0.3.1")
    rig.release("v0.3.1", message="v0.3.1, again")  # another commit, pyproject 0.3.1, signed
    moved = rig.git("rev-parse", "v0.3.1^{}")
    said = rf"^v0.3.1 moved in release_repo: the server took it at commit {first}, and release_repo"
    for _ in range(2):
        refused(server.plan(), f"{said} now has it at {moved};")
    job = uh.start(server.paths, "x")
    assert uh.git(job, "rev-parse", "refs/tags/v0.3.1").strip() == taken


def test_a_tag_signed_again_for_the_same_commit_replaces_the_servers_copy(server):
    """Review of #889, N1: a release signed again for the same commit by a key of
    `allowed_signers`, as when `release_repo` moves from the maintainer's tags to a mirror of
    the deployer's own (BOOTSTRAP §9), changes no code: the server takes the new signature."""
    rig = server.rig
    rig.release("v0.3.1")
    assert server.plan()["ok"] is True
    for name in ("v0.3.0", "v0.3.1"):  # the base too: its commit is still deploys.jsonl's
        commit = rig.git("rev-parse", f"{name}^{{}}")
        rig.git("tag", "-d", name)
        key = f"user.signingkey={rig.keys['drew']}"
        rig.git("-c", "gpg.format=ssh", "-c", key, "tag", "-s", "-m", "mine", name, commit)
    assert server.plan()["ok"] is True
    job = uh.start(server.paths, "x")
    for name in ("v0.3.0", "v0.3.1"):
        assert uh.git(job, "rev-parse", f"refs/tags/{name}").strip() == rig.git(
            "rev-parse", f"refs/tags/{name}"
        )


def test_a_base_resigned_at_another_commit_is_refused(server):
    """Codex round 1 on #889: the running release's tag, signed again upstream at another
    commit, would give the review another base than the server runs: its commit must be the one
    deploy.sh's record has for it, as `base_is_running` checks (#587)."""
    rig = server.rig
    rig.release("v0.3.1")
    ran = rig.git("rev-parse", "v0.3.0^{commit}")
    rig.git("tag", "-d", "v0.3.0")
    rig.release("v0.3.0", message="v0.3.0, again")  # signed, pyproject 0.3.0, another commit
    other = rig.git("rev-parse", "v0.3.0^{commit}")
    refused(server.plan(), rf"^v0.3.0 is {other} in release_repo, but {ran} in .*deploys.jsonl")
    (server.opt / "deploys.jsonl").unlink()
    refused(server.plan(), r"^v0.3.0 is [0-9a-f]{40} in release_repo, but no commit in ")


def test_without_release_repo_the_in_band_upgrade_is_off(server):
    rig = server.rig
    text = (rig.etc / "admin.toml").read_text()
    (rig.etc / "admin.toml").write_text(text.replace(f'release_repo = "{REPO}"\n', ""))
    server.paths = uh.Paths(**{**_fields(server.paths), "fetch_url": ""})
    rig.release("v0.3.1")
    refused(server.plan(), r"^admin.toml has no release_repo: the in-band upgrade is off")


def _fields(paths: uh.Paths) -> dict:
    return {name: getattr(paths, name) for name in uh.Paths.__dataclass_fields__}


def test_over_ssh_the_deploy_key_must_be_there(server):
    """Without the test's seam, the fetch is over SSH with the deploy key, which a deployer
    makes on the server: none is a refusal, before any connection."""
    server.paths = uh.Paths(**{**_fields(server.paths), "fetch_url": ""})
    server.rig.release("v0.3.1")
    refused(server.plan(), r"^no deploy key at .*release-key: a deployer makes it")


def test_the_ssh_command_holds_fixed_paths_and_no_agent(server, tmp_path):
    key = tmp_path / "release-key"
    key.write_text("not a key\n")
    job = uh.start(server.paths, "P-7.plan")
    command = uh.ssh_command(job)
    hosts = f"{job.current}/workshop/adminserver/github-known-hosts"
    assert command.startswith(f"/usr/bin/ssh -F /dev/null -i {key} -o IdentitiesOnly=yes ")
    for option in ("IdentityAgent=none", f"UserKnownHostsFile={hosts}", "BatchMode=yes",
                   "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null"):  # fmt: skip
        assert f" -o {option}" in command
    odd = uh.Paths(**{**_fields(server.paths), "key": str(tmp_path / "a key")})
    with pytest.raises(uh.Refused, match="not a plain path"):
        uh.ssh_command(uh.start(odd, "P-7.plan"))


# --- the apply --------------------------------------------------------------------------------


@pytest.fixture
def installed(bootstrapped, request):
    """A server bootstrapped with v0.3.0 by deploy.sh in its test rig (`ZAB_DEPLOY_PREFIX`): a
    copy of `bootstrapped`'s, its own, as `test_adminserver_deploy.server` makes it."""
    rig, template = bootstrapped
    name = re.sub(r"\W", "_", request.node.name)[:40]
    if rig.tmp.exists():  # a copy that failed part way, before its test
        rig.tmp.rename(tempfile.mkdtemp(prefix=f"{name}-before-", dir=rig.tmp.parent))
    shutil.copytree(template, rig.tmp, symlinks=True)
    yield copy.deepcopy(rig)
    rig.tmp.rename(tempfile.mkdtemp(prefix=f"{name}-", dir=rig.tmp.parent))


#: What deploy.sh's server half needs from the rig's environment, unprivileged.
DEPLOY_ENV = ("PATH", "ZAB_DEPLOY_PREFIX", "ZAB_DEPLOY_PYTHON", "UV_DEFAULT_INDEX", "UV_OFFLINE",
              "UV_CACHE_DIR", "ZAB_DEPLOY_SYSTEMCTL")  # fmt: skip


class Upgrade:
    """The helpers on the rig's server: its /etc/zab-admin and /opt/zab-admin, under its prefix,
    deploy.sh's server half run with the rig's environment (`Paths.deploy_env`)."""

    def __init__(self, rig: Rig, tmp: pathlib.Path) -> None:
        self.rig, self.state = rig, tmp / "state"
        (self.state / "requests").mkdir(parents=True, mode=0o700)
        (self.state / "results").mkdir()
        env = tuple((name, rig.env[name]) for name in DEPLOY_ENV if name in rig.env)
        self.paths = uh.Paths(
            state=str(self.state), etc=str(rig.prefix / "etc" / "zab-admin"),
            key=str(tmp / "release-key"), opt=str(rig.opt), fetch_url=str(rig.repo),
            deploy_env=env,
        )  # fmt: skip

    def ask(self, action: str, **fields) -> dict:
        body = {"version": 1, "plan": "P-7", "nonce": NONCE, "release": "v0.3.1",
                **fields}  # fmt: skip
        body.setdefault("sha256", reviewed_sha256(self.rig, body["release"], since="v0.3.0"))
        (self.state / "requests" / f"{action}.json").write_text(json.dumps(body))
        code = uh.run(action, self.paths, os.getuid())
        found = json.loads((self.state / "results" / f"{body['plan']}.{action}.json").read_text())
        assert code == (0 if found["ok"] else 1) and found["nonce"] == NONCE
        return found

    def blob(self, version: str, path: str) -> bytes:
        """`path` as `version` commits it, byte for byte."""
        argv = ["git", "-C", str(self.rig.repo), "cat-file", "blob", f"{version}:{path}"]
        return call(argv, capture_output=True, env=self.rig.env, check=True).stdout


def test_the_payload_is_the_signed_deploy_sh_byte_for_byte(installed, tmp_path):
    """Review of #894, B1: root runs the signed blob, never a text-mode copy of it. Here the
    signed deploy.sh holds a carriage return inside a comment, which bash reads as part of the
    comment and a text mode would make a line of its own, a command run as root; and a byte
    that isn't UTF-8, which a text mode would replace. In-band, the release installs, and the
    command never runs."""
    rig = installed
    script = pathlib.Path("workshop/adminserver/deploy.sh")
    injected = b"export LC_ALL=C # a comment\recho IN-BAND-INJECTED-RAN >&2\n# not UTF-8: \xff\n"

    def edit(repo: pathlib.Path) -> None:
        data = (repo / script).read_bytes()
        assert data.count(b"\nexport LC_ALL=C\n") == 1
        (repo / script).write_bytes(data.replace(b"\nexport LC_ALL=C\n", b"\n" + injected))

    rig.release("v0.3.1", edit=edit)
    up = Upgrade(rig, tmp_path)
    plan = up.ask(uh.PLAN)
    assert plan["ok"] is True, plan["message"]
    found = up.ask(uh.APPLY, commit=plan["commit"])
    assert found["ok"] is True and found["current"] == "v0.3.1", found
    assert not any("IN-BAND-INJECTED-RAN" in line for line in found["log"])
    payload = up.state / "work" / "P-7.apply" / "payload" / "deploy.sh"
    assert payload.read_bytes() == up.blob("v0.3.1", str(script))
    assert injected in payload.read_bytes()


def test_the_apply_reads_the_tags_the_plan_took_never_the_fetched_ones(installed, tmp_path):
    """Review of #894, N1: a later plan's fetch moves `refs/fetched/`, never the copies the plan
    took (`refs/tags/`), which the apply checks: here release_repo's tag moved to another
    commit after P-7, a second plan refused it, and P-7 still installs what it showed."""
    rig = installed
    rig.release("v0.3.1")
    up = Upgrade(rig, tmp_path)
    plan = up.ask(uh.PLAN)
    assert plan["ok"] is True, plan["message"]
    rig.git("tag", "-d", "v0.3.1")
    rig.release("v0.3.1", message="v0.3.1, moved")
    refused(up.ask(uh.PLAN, plan="P-8"), r"^v0.3.1 moved in release_repo")
    found = up.ask(uh.APPLY, commit=plan["commit"])
    assert found["ok"] is True and rig.records()[-1]["commit"] == plan["commit"], found


def test_the_apply_checks_the_base_and_the_commit_again(installed, tmp_path):
    """Review of #894, N1: the base must still be deploys.jsonl's commit for the running
    release, and the release's tag the commit asked for, whatever the plan's record says."""
    rig = installed
    rig.release("v0.3.1")
    up = Upgrade(rig, tmp_path)
    plan = up.ask(uh.PLAN)
    assert plan["ok"] is True, plan["message"]
    record = up.state / "results" / "P-7.plan.json"
    kept = record.read_text()
    record.write_text(kept.replace(plan["commit"], "1" * 40))
    refused(up.ask(uh.APPLY, commit="1" * 40), rf"^v0.3.1 is {plan['commit']} here, not 1{{40}}$")
    record.write_text(kept)
    deploys = rig.opt / "deploys.jsonl"
    deploys.write_text(deploys.read_text().replace(plan["base_commit"], "2" * 40))
    refused(up.ask(uh.APPLY, commit=plan["commit"]), r"^v0.3.0 is not the commit its plan and")
    assert rig.current().endswith("v0.3.0")


def test_without_release_repo_the_apply_is_off_too(server):
    """Review of #894, N4: removing release_repo stops an apply too, whatever was planned."""
    rig = server.rig
    text = (rig.etc / "admin.toml").read_text()
    (rig.etc / "admin.toml").write_text(text.replace(f'release_repo = "{REPO}"\n', ""))
    server.paths = uh.Paths(**{**_fields(server.paths), "fetch_url": ""})
    server.request(uh.APPLY, sha256="0" * 64, commit="0" * 40)
    assert uh.run(uh.APPLY, server.paths, os.getuid()) == 1
    found = json.loads((server.state / "results" / "P-7.apply.json").read_text())
    assert found["message"].startswith("admin.toml has no release_repo: the in-band upgrade is off")


def test_an_approved_plan_is_installed_in_band(installed, tmp_path):
    """The apply runs the release's own deploy.sh --server --in-band: the build, the switch,
    the record with the plan, every unit's restart but the bots'."""
    rig = installed
    log = systemctl_log(rig)
    rig.release("v0.3.1")
    up = Upgrade(rig, tmp_path)
    plan = up.ask(uh.PLAN)
    assert plan["ok"] is True, plan["message"]
    found = up.ask(uh.APPLY, commit=plan["commit"])
    assert found["ok"] is True, found
    assert (found["switched"], found["current"]) == (True, "v0.3.1")
    assert rig.current().endswith("v0.3.1")
    _, wheel, record = rig.records()  # after the bootstrap, the wheel, then the upgrade (#569)
    assert (wheel["event"], wheel["version"]) == ("wheel", "v0.3.1")
    # #901's review, N4: the placed wheel's SHA-256, for the deployer to compare with theirs
    assert found["wheel_sha256"] == wheel["sha256"]
    assert found["message"].startswith(
        f"v0.3.1 is installed and current, with its fleet wheel (SHA-256 {wheel['sha256']}), "
    )
    assert (rig.prefix / WHEELS / wheel["wheel"]).is_file()
    assert (record["event"], record["plan"], record["commit"]) == ("upgrade", "P-7", plan["commit"])
    assert record["downgrade"] is False  # the apply never passes --downgrade (no in-band ones)
    assert (record["base"], record["diff_sha256"]) == ("v0.3.0", plan["diff_sha256"])
    others = [step for step in UNIT_STEPS if step != "restart zab-admin-bot.service"]
    assert log.read_text().splitlines()[-len(others) :] == others


def test_an_apply_holds_to_its_plans_record(installed, tmp_path):
    """The apply trusts the request's release, commit and SHA-256 only as far as the plan
    helper's own record of that plan has them, from the release that runs now."""
    rig = installed
    rig.release("v0.3.1")
    up = Upgrade(rig, tmp_path)
    no_plan = up.ask(uh.APPLY, commit="0" * 40)
    refused(no_plan, r"^P-7 has no check on record \(.*P-7\.plan\.json\): request it again$")
    plan = up.ask(uh.PLAN)
    assert plan["ok"] is True, plan["message"]
    for change, name in (({"commit": "1" * 40}, "commit"), ({"sha256": "2" * 64}, "diff_sha256")):
        found = up.ask(uh.APPLY, **{"commit": plan["commit"], **change})
        refused(found, rf"^the request's {name} is not the one P-7's check recorded$")
    found = up.ask(uh.APPLY, commit=plan["commit"], downgrade=False)
    refused(found, r"^an in-band upgrade only goes forward: go back with deploy.sh --downgrade")
    record = up.state / "results" / "P-7.plan.json"
    record.write_text(record.read_text().replace('"running": "v0.3.0"', '"running": "v0.2.9"'))
    refused(up.ask(uh.APPLY, commit=plan["commit"]), r"^P-7 was planned from v0.2.9, and the")
    assert rig.current().endswith("v0.3.0") and len(rig.records()) == 1


def test_a_deploy_that_fails_before_the_switch_changes_nothing(installed, tmp_path):
    rig = installed
    rule = pathlib.Path("workshop/adminserver/nftables-stop-imds.nft")
    rig.release("v0.3.1", edit=lambda repo: (repo / rule).unlink())  # install_system fails
    up = Upgrade(rig, tmp_path)
    plan = up.ask(uh.PLAN)
    assert plan["ok"] is True, plan["message"]
    found = up.ask(uh.APPLY, commit=plan["commit"])
    refused(found, r"^deploy.sh failed \(exit 1\) before the switch: v0.3.0 runs, as before: ")
    assert found["switched"] is False and found["log"], found
    assert rig.current().endswith("v0.3.0")
    assert [r["event"] for r in rig.records()] == ["bootstrap", "wheel"]  # a wheel is harmless


def test_a_wheel_that_does_not_build_installs_nothing(installed, tmp_path):
    """#569: the fleet wheel is built first, so a build its checks refuse (here a build hook,
    which build_config refuses as on the laptop) leaves the server as it was."""
    rig = installed
    hook = '\n[tool.hatch.build.hooks.custom]\npath = "src/zab/hook.py"\n'
    project = pathlib.Path("pyproject.toml")
    rig.release("v0.3.1", edit=lambda repo: (repo / project).write_text(
        (repo / project).read_text() + hook))  # fmt: skip
    up = Upgrade(rig, tmp_path)
    plan = up.ask(uh.PLAN)
    assert plan["ok"] is True, plan["message"]
    found = up.ask(uh.APPLY, commit=plan["commit"])
    refused(found, r"^the fleet wheel was not built \(exit 1\), so nothing was installed: v0.3.0")
    assert "configures the build beyond" in found["message"]
    assert rig.current().endswith("v0.3.0") and len(rig.records()) == 1
    assert not (rig.prefix / WHEELS).exists() or not list((rig.prefix / WHEELS).iterdir())


def test_an_apply_request_names_its_commit(server):
    server.request(uh.APPLY, sha256="0" * 64)
    assert uh.run(uh.APPLY, server.paths, os.getuid()) == 1
    found = json.loads((server.state / "results" / "P-7.apply.json").read_text())
    keys = "commit, nonce, plan, release, sha256, version"
    assert f"keys are not {keys}" in found["message"]


# --- the request --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"version": 2}, "not version 1"),
        ({"version": True}, "not version 1"),
        ({"release": "main"}, "release is not one"),
        ({"release": "v0.3.1 --upload-pack=x"}, "release is not one"),
        ({"release": "v01.3.1"}, "release is not one"),
        ({"downgrade": True}, "only goes forward"),
        ({"downgrade": False}, "only goes forward"),
        ({"commit": "0" * 40}, "keys are not"),
        ({"sha256": "0" * 63}, "sha256 is not one"),
        ({"sha256": "A" * 64}, "sha256 is not one"),
        ({"sha256": None}, "sha256 is not one"),
    ],
)
def test_a_request_is_read_by_its_patterns_alone(server, change, why):
    server.rig.release("v0.3.1")
    refused(server.plan(**change), why)


@pytest.mark.parametrize(
    "change", [{"plan": "P-0"}, {"plan": "../x"}, {"nonce": "x" * 32}, {"nonce": None}]
)
def test_a_request_whose_plan_cant_be_read_is_answered_unread(server, change):
    server.request(**change)
    assert uh.run(uh.PLAN, server.paths, os.getuid()) == 1
    found = json.loads((server.state / "results" / "unread.plan.json").read_text())
    assert found["ok"] is False and found["plan"] is None and "is not one" in found["message"]
    assert not (server.state / "requests" / "plan.json").exists()
    assert sorted(os.listdir(server.state / "results")) == ["unread.plan.json"]


def test_no_request_is_no_run(server):
    assert uh.run(uh.PLAN, server.paths, os.getuid()) == 0
    assert os.listdir(server.state / "results") == []


def test_a_link_for_a_request_is_removed_unread(server, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text('{"version": 1}')
    (server.state / "requests" / "plan.json").symlink_to(secret)
    assert uh.run(uh.PLAN, server.paths, os.getuid()) == 1
    assert not os.path.lexists(server.state / "requests" / "plan.json") and secret.exists()


def test_a_request_directory_others_may_write_is_refused(server):
    (server.state / "requests").chmod(0o777)
    server.request()
    assert uh.run(uh.PLAN, server.paths, os.getuid()) == 1
    found = json.loads((server.state / "results" / "unread.plan.json").read_text())
    assert "is not zab-admin's alone" in found["message"]


def test_the_outcome_is_the_bots_to_read_and_the_repository_roots_alone(server):
    server.rig.release("v0.3.1")
    assert server.plan()["ok"] is True
    result = server.state / "results" / "P-7.plan.json"
    assert stat.S_IMODE(result.stat().st_mode) == 0o644
    assert stat.S_IMODE((server.state / "repo.git").stat().st_mode) == 0o700
    attributes = (server.state / "repo.git" / "info" / "attributes").read_text()
    assert attributes == uh.ATTRIBUTES
    names = uh.git(uh.start(server.paths, "x"), "for-each-ref", "--format=%(refname)").split()
    assert names == [  # the two tags, as fetched and as taken, nothing else
        "refs/fetched/v0.3.0", "refs/fetched/v0.3.1", "refs/tags/v0.3.0", "refs/tags/v0.3.1",
    ]  # fmt: skip


def test_old_outcomes_are_pruned(server):
    for n in range(uh.KEEP + 5):
        (server.state / "results" / f"P-{n + 100}.plan.json").write_text("{}")
    server.rig.release("v0.3.1")
    server.plan()
    assert len(os.listdir(server.state / "results")) == uh.KEEP
    assert (server.state / "results" / "P-7.plan.json").exists()


# --- the plan helper's sandbox ----------------------------------------------------------------

#: In the unit's namespace: each path's access (rw, ro, or hidden: absent or unreadable).
SANDBOX_PROBE = """
import errno, json, os, sys

found = {}
for path in sys.argv[1:]:
    try:
        if os.path.isdir(path):
            os.close(os.open(path + "/.probe", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            os.unlink(path + "/.probe")
        else:
            with open(path, "rb") as f:
                f.read(1)
            os.close(os.open(path, os.O_WRONLY | os.O_APPEND))
        found[path] = "rw"
    except OSError as exc:
        found[path] = "ro" if exc.errno == errno.EROFS else "hidden"
print(json.dumps(found))
"""
SANDBOX_KINDS = ("ReadWritePaths", "TemporaryFileSystem", "BindReadOnlyPaths", "InaccessiblePaths")


@pytest.mark.skipif(not _user_manager(), reason="no user systemd manager with namespaces")
def test_the_plan_helper_sees_the_deploy_key_and_none_of_what_it_hides(tmp_path):
    """The unit's mounts, rooted in a test tree, for a service of the user manager: the helper
    writes its own state alone, reads admin.toml and the deploy key, and sees no other key of
    secrets/, nothing of the bot's state (the fleet key above all), and none of the other
    secrets it hides: the console's, Caddy's, the SSH host keys, the password hashes (review of
    #893, B2)."""
    root = tmp_path / "root"
    files = {
        "etc/zab-admin/admin.toml": "x", "etc/zab-admin/allowed_signers": "x",
        "etc/zab-admin/secrets/release-key": "key", "etc/zab-admin/secrets/github-token": "t",
        "etc/zab-admin/secrets/bots/ws.zuliprc": "z", "var/lib/zab-admin/.ssh/id_ed25519": "k",
        "opt/zab-admin/current/x": "x", "var/lib/zab-console/secret": "s",
        "var/lib/caddy/key": "c", "etc/ssh/ssh_host_ed25519_key": "h", "etc/shadow": "p",
        "etc/gshadow": "g",
    }  # fmt: skip
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    for name in ("requests", "results"):
        (root / "var/lib/zab-admin-upgrade" / name).mkdir(parents=True)
    argv = ["systemd-run", "--user", "--wait", "--pipe", "--quiet", "-p", "ProtectSystem=strict"]
    unit = (SERVER / "zab-admin-upgrade-plan.service").read_text()
    for line in unit.splitlines():
        kind, _, value = line.partition("=")
        if kind in SANDBOX_KINDS:
            words = " ".join(_rooted(word, str(root)) for word in value.split())
            argv += ["-p", f"{kind}={words}"]
    state = root / "var/lib/zab-admin-upgrade"
    probed = [*(root / name for name in files), state, state / "requests", state / "results"]
    argv += ["--", sys.executable, "-I", "-S", "-c", SANDBOX_PROBE, *map(str, probed)]
    done = call(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    found = json.loads(done.stdout)
    seen = {pathlib.Path(path).relative_to(root).as_posix(): how for path, how in found.items()}
    assert seen == {
        "etc/zab-admin/admin.toml": "ro", "etc/zab-admin/allowed_signers": "ro",
        "etc/zab-admin/secrets/release-key": "ro",  # the one secret it reads
        "etc/zab-admin/secrets/github-token": "hidden",
        "etc/zab-admin/secrets/bots/ws.zuliprc": "hidden",
        "var/lib/zab-admin/.ssh/id_ed25519": "hidden", "opt/zab-admin/current/x": "ro",
        "var/lib/zab-console/secret": "hidden", "var/lib/caddy/key": "hidden",
        "etc/ssh/ssh_host_ed25519_key": "hidden", "etc/shadow": "hidden", "etc/gshadow": "hidden",
        "var/lib/zab-admin-upgrade": "rw", "var/lib/zab-admin-upgrade/requests": "rw",
        "var/lib/zab-admin-upgrade/results": "rw",
    }  # fmt: skip
