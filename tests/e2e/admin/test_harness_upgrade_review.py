"""A10b's review case (`harness/upgrade.py`, #963), offline: the review's text and its reading
on both sides, then the case itself on the development machine, with this tree's plan helper and
deploy.sh (`workshop/test_adminserver_upgrade.Server`), through the harness's own functions."""

from __future__ import annotations

import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from e2e.admin.harness import upgrade
from e2e.admin.test_scenarios_1_upgrade import RESULTS, _shown_alike
from e2e.harness.vm import Result
from workshop.test_adminserver_deploy import DEPLOY, Rig, _tools_missing
from workshop.test_adminserver_deploy import index as index  # the fixture
from workshop.test_adminserver_upgrade import Server as Helper
from zab.admin import upgradehelper as uh

needs_tools = pytest.mark.skipif(bool(_tools_missing()), reason=_tools_missing() or "ok")
COMMIT = "c" * 40


class Here:
    """The server, here: its scripts run by this machine's bash, with its perl."""

    def script(self, literal, *args, input=None, **_):
        argv = ["bash", "-c", literal, "bash", *args]
        done = subprocess.run(argv, input=input, capture_output=True, text=True, check=False)  # noqa: S603
        return Result(done.returncode, done.stdout, done.stderr)


def test_hidden_holds_each_class_and_the_helper_shows_it_as_shown():
    """HIDDEN holds the two non-ASCII characters both sides keep, characters of each class both
    make visible, a line that isn't UTF-8 and no NUL; the plan helper's `sanitized()` shows it as
    SHOWN, line for line."""
    assert uh.sanitized(upgrade.HIDDEN) == list(upgrade.SHOWN)
    text = upgrade.HIDDEN.decode("utf-8", "replace")
    for c in "\u00b7\u2014\x1b\x07\x0b\x0c\r\x7f\x85\u200b\u202e\ufeff\u2028\u2029\xa0\u3164\ue000":
        assert c in text, ascii(c)
    with pytest.raises(UnicodeDecodeError):
        upgrade.HIDDEN.decode("utf-8")
    assert b"\0" not in upgrade.HIDDEN
    assert upgrade.HIDDEN.count(b"\n") == len(upgrade.SHOWN) and upgrade.HIDDEN.endswith(b"\n")
    lines = upgrade.HIDDEN.split(b"\n")
    # #963's review: C0 controls on a non-ASCII line too (N1), and encoded surrogates (B1)
    assert not lines[0].isascii() and b"\r" in lines[0] and b"\x1b" in lines[0]
    assert b"\xed\xa0\x80" in upgrade.HIDDEN and b"\xf4\x90" not in upgrade.HIDDEN


@pytest.mark.parametrize("version", ["v0.2.20rc4", "v0.2.20"])
def test_the_changelog_has_the_one_section_a_plan_shows(monkeypatch, version):
    """The good rc's CHANGELOG.md has one section, the one the plan helper's `changelog` takes
    for an rc as for a release (`## 0.2.20 …`), shown as `shown_section`; and HIDDEN goes in
    the privileged file as it is."""
    files = upgrade.hidden_files(version)
    reviewed = upgrade.review_file(version)
    assert set(files) == {reviewed, "CHANGELOG.md"} and files[reviewed] == upgrade.HIDDEN
    # privileged, so the plan shows it; and the release's own (#963's review, B2)
    assert reviewed == f"workshop/adminserver/a10b-review-{version[1:]}.md"
    monkeypatch.setattr(uh, "git_bytes", lambda job, *args: files["CHANGELOG.md"])
    shown = uh.changelog(None, COMMIT, version)  # type: ignore[arg-type]
    assert shown[0] == "## 0.2.20 \u2014 A10b's review (#963)"
    assert "\n".join(shown) + "\n" == upgrade.shown_section(version)
    assert upgrade.section(version).endswith(b"\n\n" + upgrade.HIDDEN)


def test_the_plans_review_and_changelog_are_what_plan_text_put_there():
    """`planned_review` and `planned_changelog` take back what the helper's `plan_text` wrote:
    the review up to its unprivileged rest, and the CHANGELOG section."""
    review = (
        "zab admin release review: v0.3.0..cccccccccccc (" + COMMIT + ")\n\n"
        "==== The privileged files (1): read every line ====\n"
        + "".join(f"+{line}\n" for line in upgrade.SHOWN)
        + "\n==== The rest (1 files) ====\n+rest\n"
    )  # fmt: skip
    tags = [uh.Tag(f"v0.3.{n}", "a" * 40, COMMIT, "e@example.org", "ED25519 key SHA256:x")
            for n in (0, 1)]  # fmt: skip
    seen = uh.Review("1" * 64, "2" * 64, 3, (f"A {upgrade.review_file('v0.3.1')}",), review)
    text = uh.plan_text(tags[0], tags[1], seen, list(upgrade.SHOWN), "render_policy.py: same")
    assert upgrade.planned_review(text) == review.split(uh.REST)[0].rstrip("\n")
    assert upgrade.planned_changelog(text, "v0.3.1") == "\n".join(upgrade.SHOWN) + "\n"
    whole = uh.Review("1" * 64, "2" * 64, 3, (), "no rest\n\n")  # a review without its rest
    text = uh.plan_text(tags[0], tags[1], whole, ["## 0.3.1"], "render_policy.py: same")
    assert upgrade.planned_review(text) == "no rest"
    with pytest.raises(ValueError, match="no review"):
        upgrade.planned_review("==== CHANGELOG.md, 0.3.1 ====\n")
    with pytest.raises(ValueError, match=r"no CHANGELOG section for v0\.3\.2"):
        upgrade.planned_changelog(text, "v0.3.2")


def test_the_deployers_review_is_its_bytes_up_to_the_rest(tmp_path):
    """The deployer's `.review`, named for its base and commit as deploy.sh names it, read as
    bytes and decoded strictly, cut where the plan cuts it."""
    path = tmp_path / f"zab-admin-v0.3.0..{COMMIT[:12]}.review"
    path.write_bytes(f"head\n+{upgrade.SHOWN[0]}\n\n\n==== The rest (0 files) ====\n".encode())
    assert upgrade.reviewed(tmp_path, "v0.3.0", COMMIT) == f"head\n+{upgrade.SHOWN[0]}"
    path.write_bytes(b"not sanitized: \xff\n")
    with pytest.raises(UnicodeDecodeError):
        upgrade.reviewed(tmp_path, "v0.3.0", COMMIT)
    with pytest.raises(FileNotFoundError):
        upgrade.reviewed(tmp_path, "v0.3.0", "d" * 40)


def test_a_difference_is_named_by_its_first_line_in_ascii():
    names = ("the plan's", "the deployer's")
    said = upgrade.first_difference("a\nb\u00b7\nc", "a\nb\nc", names)
    assert said == "line 2: the plan's 'b\\xb7', the deployer's 'b'"
    said = upgrade.first_difference("a", "a\n", names)
    assert said == "line 2: the plan's (none: the text ends), the deployer's ''"


def test_sanitizes_program_is_deploy_shs_own():
    program = upgrade.sanitize_program(DEPLOY.read_text())
    assert program.startswith("BEGIN { binmode STDOUT") and "<U+%04X>" in program
    for text in ("", "#!/bin/sh\nsanitize() {\n  cat -v\n}\nx() {\n  perl -ne 'print'\n}\n"):
        with pytest.raises(ValueError, match="no sanitize"):
            upgrade.sanitize_program(text)


def test_the_versions_that_show_text_are_noted():
    """The server's perl and the plan helper's python3, and the development machine's perl,
    each as it answered, `?` for none."""

    class There:
        def run(self, argv, check=True, **_):
            if argv == upgrade.PERL_UNICODE:
                return Result(0, "perl v5.38.2, Unicode 15.0.0\n", "")
            return Result(127, "", "no python3")

    def here(argv, **_):
        assert argv == upgrade.PERL_UNICODE
        return Result(0, "perl v5.40.1, Unicode 15.1.0", "")

    said = upgrade.unicode_versions(There(), run=here)
    assert said == ("the server: perl v5.38.2, Unicode 15.0.0, ?; the development machine: "
                    "perl v5.40.1, Unicode 15.1.0")  # fmt: skip


@needs_tools
def test_the_version_commands_answer_here():
    done = subprocess.run(upgrade.PERL_UNICODE, capture_output=True, text=True, check=True)  # noqa: S603
    assert re.fullmatch(r"perl v\d+\.\d+\.\d+, Unicode \d+\.\d+\.\d+", done.stdout), done.stdout
    if shutil.which("/usr/bin/python3"):
        done = subprocess.run(upgrade.PYTHON_UNICODE, capture_output=True, text=True, check=True)  # noqa: S603
        assert re.fullmatch(r"python3 \d+\.\d+\.\d+\S*, Unicode \d+\.\d+\.\d+\n", done.stdout)


@needs_tools
def test_deploy_shs_sanitize_on_a_servers_perl_shows_hidden_as_shown():
    """`sanitized_on`'s script, run here: deploy.sh's own `sanitize` of HIDDEN, and of each
    CHANGELOG section, is SHOWN, as the helper shows it."""
    program = upgrade.sanitize_program(DEPLOY.read_text())
    assert upgrade.sanitized_on(Here(), program, upgrade.HIDDEN) == "\n".join(upgrade.SHOWN) + "\n"
    for version in ("v0.2.20rc4", "v0.2.20"):
        found = upgrade.sanitized_on(Here(), program, upgrade.section(version))
        assert found == upgrade.shown_section(version)


def _hidden(version: str, dropped: str = ""):
    """An edit of the rig's tree: `version`'s good-rc files written, and `dropped`'s review
    file gone, as a good rc made on the commit under test lacks an earlier one's."""

    def edit(repo):
        if dropped:
            (repo / upgrade.review_file(dropped)).unlink()
        for path, data in upgrade.hidden_files(version).items():
            (repo / path).write_bytes(data)

    return edit


def _helper(tmp_path, index, again: bool) -> Helper:
    (tmp_path / "rig").mkdir()
    rig = Rig(tmp_path / "rig", index)
    rig.release("v0.3.0", edit=_hidden("v0.3.0") if again else None)
    server = Helper(tmp_path, rig)
    rig.release("v0.3.1", edit=_hidden("v0.3.1", "v0.3.0" if again else ""))
    return server


@pytest.fixture
def helper(tmp_path, index):
    """The plan helper, with v0.3.0 running, and a release v0.3.1 holding HIDDEN (#963)."""
    return _helper(tmp_path, index, again=False)


@pytest.fixture
def again(tmp_path, index):
    """The same, after an earlier A10b: the running v0.3.0 is its good rc, with its own
    review file, which v0.3.1, made on the commit under test, lacks (#963's review, B2)."""
    return _helper(tmp_path, index, again=True)


@needs_tools
def test_a_plan_shows_the_review_of_hidden_text_as_the_deployer_has_it(helper):
    """A10b's case, here: the plan's review is the deployer's `.review` byte for byte, with
    HIDDEN's file among the privileged ones, as SHOWN; its CHANGELOG section is deploy.sh's
    `sanitize` of the same bytes."""
    found = helper.plan()
    assert found["ok"] is True, found["message"]
    commit = helper.rig.git("rev-parse", "v0.3.1^{commit}")
    planned = upgrade.planned_review(found["text"])
    theirs = upgrade.reviewed(helper.rig.tmp / "out", "v0.3.0", commit)
    names = ("the plan's", "the deployer's")
    assert planned.encode() == theirs.encode(), upgrade.first_difference(planned, theirs, names)
    added = re.escape(upgrade.review_file("v0.3.1"))
    assert re.search(rf"^P A +\+10 +-0 +text +{added}$", planned, re.M), planned[:900]
    assert "\n" + "\n".join(f"+{line}" for line in upgrade.SHOWN) + "\n" in planned
    section = upgrade.planned_changelog(found["text"], "v0.3.1")
    program = upgrade.sanitize_program(DEPLOY.read_text())
    assert section == upgrade.sanitized_on(Here(), program, upgrade.section("v0.3.1"))
    assert section == upgrade.shown_section("v0.3.1")


def _c0_kept_on_a_non_ascii_line(real):
    """`shown`, but for its non-ASCII branch, which keeps C0 controls raw (#963's review, N1:
    the mutant HIDDEN's first line now catches)."""

    def shown(line: str) -> str:
        if line.isascii():
            return real(line)
        return "".join(c if ord(c) < 0x20 and c != "\t" else real(c) for c in line)

    return shown


#: Each gap: how `shown()` is broken, and the first line of the CHANGELOG section it shows.
GAPS = {
    "fillers": ("_FILLERS", frozenset(), "line 9: the plan's 'Ignorable: CGJ <U+034F>, fillers "
                "\\u115f \\u3164,"),
    "c0": ("shown", _c0_kept_on_a_non_ascii_line(uh.shown), "line 3: the plan's 'Non-ASCII "
           "text, kept: a middle dot \\xb7 and an em dash \\u2014; on their line, CR \\r and "
           "ESC \\x1b[1m.'"),
}  # fmt: skip


@needs_tools
@pytest.mark.parametrize("gap", sorted(GAPS))
def test_a_gap_in_shown_is_caught_by_the_changelog_not_the_review(helper, monkeypatch, gap):
    """Why A10b compares the CHANGELOG section too: the review reaches `shown()` already
    sanitized by deploy.sh, so a character `shown()` failed to flag would still match there;
    the CHANGELOG section, which `shown()` alone shows, differs, and says where: the Hangul
    fillers, or a C0 control on a non-ASCII line."""
    name, value, line = GAPS[gap]
    monkeypatch.setattr(uh, name, value)
    found = helper.plan()
    commit = helper.rig.git("rev-parse", "v0.3.1^{commit}")
    planned = upgrade.planned_review(found["text"])
    assert planned == upgrade.reviewed(helper.rig.tmp / "out", "v0.3.0", commit)
    section = upgrade.planned_changelog(found["text"], "v0.3.1")
    program = upgrade.sanitize_program(DEPLOY.read_text())
    perl = upgrade.sanitized_on(Here(), program, upgrade.section("v0.3.1"))
    assert section != perl
    assert upgrade.first_difference(section, perl, ("the plan's", "sanitize's")).startswith(line)


class Served(Here):
    """The test server as A10b's check reaches it, here: the plan helper's results read from
    the helper's state, the commands run by this machine."""

    def __init__(self, helper) -> None:
        self.results = helper.state / "results"

    def read(self, path: str) -> str:
        assert path.startswith(RESULTS + "/"), path
        return (self.results / path.removeprefix(RESULTS + "/")).read_text()

    def run(self, argv, check=True, **_):
        done = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        return Result(done.returncode, done.stdout, done.stderr)


@needs_tools
def test_a10bs_check_passes_on_a_plan_shown_alike_and_fails_on_a_gap(helper, monkeypatch):
    """A10b's own check (`_shown_alike`), on the helper's plan and the deployer's review made
    here: it passes, and notes the versions and the bytes compared; with a character `shown()`
    no longer flags, it fails on the CHANGELOG section, naming the line."""
    found = helper.plan()
    commit = helper.rig.git("rev-parse", "v0.3.1^{commit}")
    scenario, out = SimpleNamespace(notes=[]), helper.rig.tmp / "out"
    release, deploy_sh = ("v0.3.1", commit), DEPLOY.read_text()
    _shown_alike(scenario, Served(helper), "P-7", release, "v0.3.0", deploy_sh, out)
    versions, compared = scenario.notes
    assert re.match(r"#963, what shows the plan's text: the server: perl v\d", versions), versions
    assert re.fullmatch(r"P-7: its review, .* byte for byte \(\d+ bytes\); its CHANGELOG .*",
                        compared)  # fmt: skip
    assert found["ok"] is True
    monkeypatch.setattr(uh, "_FILLERS", frozenset())
    helper.plan()
    with pytest.raises(AssertionError, match=r"line 9: the plan's 'Ignorable: .*the server's san"):
        _shown_alike(scenario, Served(helper), "P-7", release, "v0.3.0", deploy_sh, out)


@needs_tools
def test_a10b_again_after_a_passing_one_reviews_hidden_again(again):
    """#963's review, B2: A10b run again (`--scenarios A10b`), once an earlier one passed, on
    the server it left: the running release is that A10b's good rc, which holds its own review
    file. The next good rc adds its own, `P A` with HIDDEN's lines as `+`, and the earlier
    one's is `P D`, its lines `-`, shown alike: A10b's check passes as on a first run."""
    found = again.plan()
    assert found["ok"] is True, found["message"]
    commit = again.rig.git("rev-parse", "v0.3.1^{commit}")
    planned = upgrade.planned_review(found["text"])
    added, gone = (re.escape(upgrade.review_file(v)) for v in ("v0.3.1", "v0.3.0"))
    assert re.search(rf"^P A +\+10 +-0 +text +{added}$", planned, re.M), planned[:900]
    assert re.search(rf"^P D +\+0 +-10 +text +{gone}$", planned, re.M), planned[:900]
    assert "\n" + "\n".join(f"-{line}" for line in upgrade.SHOWN) + "\n" in planned
    scenario, out = SimpleNamespace(notes=[]), again.rig.tmp / "out"
    release, deploy_sh = ("v0.3.1", commit), DEPLOY.read_text()
    _shown_alike(scenario, Served(again), "P-7", release, "v0.3.0", deploy_sh, out)
    assert len(scenario.notes) == 2
