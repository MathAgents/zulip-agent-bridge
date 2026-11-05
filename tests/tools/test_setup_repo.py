"""`zab setup-repo` (SPEC §13.1): the six deploy-key steps against a fake runner and a temp HOME."""

from __future__ import annotations

import io
import os
import stat

import pytest

from zab.tools import setup_repo as sr
from zab.tools.common import RunResult

from .fakes import FakeRunner, contains, fail, ok


def keygen_side_effect(argv: list[str]) -> None:
    path = argv[argv.index("-f") + 1]
    with open(path, "w") as f:
        f.write("PRIVATE")
    with open(path + ".pub", "w") as f:
        f.write("ssh-ed25519 AAAAC3fake zab-box-repo\n")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("alice/thesis", ("alice", "thesis")), (" bob-1/my.repo_2 ", ("bob-1", "my.repo_2"))],
)
def test_parse_repo(spec, expected):
    assert sr.parse_repo(spec) == expected


@pytest.mark.parametrize("bad", ["thesis", "a/b/c", "alice/thesis.git", "al ice/x", "/x", "a/"])
def test_parse_repo_rejects(bad):
    with pytest.raises(ValueError):
        sr.parse_repo(bad)


def test_remote_url_uses_alias_host():
    assert sr.remote_url("alice", "thesis") == "git@github.com-thesis:alice/thesis.git"


def test_ensure_key_generates_once(tmp_path):
    home = str(tmp_path)
    run = FakeRunner(ok(["ssh-keygen"], side_effect=keygen_side_effect))
    pub = sr.ensure_key(run, home, "thesis", "box")
    assert pub.startswith("ssh-ed25519 ")
    argv = run.argvs()[0]
    assert argv[:2] == ["ssh-keygen", "-q"] and "-N" in argv and argv[argv.index("-N") + 1] == ""
    assert argv[argv.index("-t") + 1] == "ed25519"
    assert argv[argv.index("-C") + 1] == "zab-box-thesis"
    assert argv[argv.index("-f") + 1] == os.path.join(home, ".ssh", "zab_thesis")
    assert stat.S_IMODE(os.stat(tmp_path / ".ssh").st_mode) == 0o700
    # second call: the key exists, nothing is run
    assert sr.ensure_key(run, home, "thesis", "box") == pub
    assert len(run.calls) == 1


def test_ensure_key_reports_failure(tmp_path):
    run = FakeRunner(fail(["ssh-keygen"], "ssh-keygen: not found"))
    with pytest.raises(RuntimeError, match="ssh-keygen"):
        sr.ensure_key(run, str(tmp_path), "thesis", "box")


def test_ensure_ssh_config_is_idempotent_and_private(tmp_path):
    home = str(tmp_path)
    os.makedirs(tmp_path / ".ssh")
    assert sr.ensure_ssh_config(home, "alice", "thesis") is True
    cfg = tmp_path / ".ssh" / "config"
    text = cfg.read_text()
    assert "Host github.com-thesis\n" in text
    assert f"IdentityFile {home}/.ssh/zab_thesis" in text
    assert "IdentitiesOnly yes" in text and "StrictHostKeyChecking accept-new" in text
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert sr.ensure_ssh_config(home, "alice", "thesis") is False
    assert cfg.read_text().count("Host github.com-thesis") == 1
    # another repo gets its own block; an existing file without a trailing newline is fine
    cfg.write_text(cfg.read_text().rstrip("\n"))
    assert sr.ensure_ssh_config(home, "alice", "other") is True
    assert cfg.read_text().count("\nHost github.com-other\n") == 1


def test_instructions_name_the_settings_page_and_write_access():
    text = sr.instructions("ssh-ed25519 KEY", "alice", "thesis")
    assert "https://github.com/alice/thesis/settings/keys/new" in text
    assert "Allow write access" in text and "ssh-ed25519 KEY" in text


def test_clone_when_missing(tmp_path):
    path = str(tmp_path / "work" / "thesis")
    run = FakeRunner(ok(["git", "clone"]))
    assert sr.clone_or_set_remote(run, "git@github.com-thesis:alice/thesis.git", path) == "cloned"
    assert run.argvs() == [["git", "clone", "--", "git@github.com-thesis:alice/thesis.git", path]]
    assert run.calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert os.path.isdir(tmp_path / "work")


def test_set_remote_when_checkout_exists(tmp_path):
    path = tmp_path / "thesis"
    (path / ".git").mkdir(parents=True)
    run = FakeRunner(ok(["git", "-C", str(path), "remote", "get-url"]), ok(["git", "-C"]))
    assert sr.clone_or_set_remote(run, "URL", str(path)) == "remote set"
    assert run.argvs()[-1] == ["git", "-C", str(path), "remote", "set-url", "origin", "URL"]
    run2 = FakeRunner(fail(["git", "-C", str(path), "remote", "get-url"]), ok(["git", "-C"]))
    sr.clone_or_set_remote(run2, "URL", str(path))
    assert run2.argvs()[-1] == ["git", "-C", str(path), "remote", "add", "origin", "URL"]


def test_clone_failure_is_reported(tmp_path):
    run = FakeRunner(fail(["git", "clone"], "fatal: Could not read from remote repository."))
    with pytest.raises(RuntimeError, match="Could not read"):
        sr.clone_or_set_remote(run, "URL", str(tmp_path / "x"))


def test_verify_write_access_pushes_then_deletes(tmp_path):
    run = FakeRunner(ok(["git"]))
    ok_, detail = sr.verify_write_access(run, str(tmp_path), "box")
    assert ok_ and "zab-preflight-box" in detail
    a = run.argvs()
    assert a[0][-2:] == ["origin", "HEAD:refs/heads/zab-preflight-box"]
    assert a[1][-3:] == ["origin", "--delete", "zab-preflight-box"]


def test_verify_write_access_rejected_names_the_fix(tmp_path):
    run = FakeRunner(
        contains(
            "HEAD:refs/heads/",
            RunResult(
                128, "", "ERROR: The key you are authenticating with has been marked as read only."
            ),
        )
    )
    ok_, detail = sr.verify_write_access(run, str(tmp_path), "box")
    assert not ok_
    assert "Allow write access" in detail and "read only" in detail
    assert len(run.calls) == 1  # no delete attempted


def test_verify_write_access_delete_failure_is_a_warning_not_an_error(tmp_path):
    run = FakeRunner(contains("--delete", RunResult(1, "", "remote: cannot delete")), ok(["git"]))
    ok_, detail = sr.verify_write_access(run, str(tmp_path), "box")
    assert ok_ and "could not delete" in detail


def test_setup_repo_end_to_end(tmp_path):
    home = str(tmp_path)
    run = FakeRunner(ok(["ssh-keygen"], side_effect=keygen_side_effect), ok(["git"]))
    out = io.StringIO()
    waited = []
    rc = sr.setup_repo(
        "alice/thesis", run=run, home=home, host="box", out=out, wait=lambda: waited.append(1)
    )
    assert rc == 0 and waited == [1]
    text = out.getvalue()
    assert "settings/keys/new" in text and "write access OK" in text
    assert f"cloned: {home}/work/thesis" in text
    clone = next(a for a in run.argvs() if a[:2] == ["git", "clone"])
    assert clone[-2:] == ["git@github.com-thesis:alice/thesis.git", f"{home}/work/thesis"]
    # --yes skips the wait and a failing push exits 1
    run2 = FakeRunner(
        ok(["ssh-keygen"], side_effect=keygen_side_effect),
        contains("HEAD:refs/heads/", RunResult(128, "", "denied")),
        ok(["git"]),
    )
    rc = sr.setup_repo("alice/thesis", yes=True, run=run2, home=home, host="box", out=io.StringIO())
    assert rc == 1


def test_main_reports_bad_spec(capsys):
    assert sr.main(["not-a-repo"]) == 1
    assert "OWNER/REPO" in capsys.readouterr().err


def test_no_shell_strings_anywhere(tmp_path):
    """No shell strings (CONTRIBUTING.md, rule 3): every argv element is a plain string, no `sh -c`,
    no joined commands."""
    run = FakeRunner(ok(["ssh-keygen"], side_effect=keygen_side_effect), ok(["git"]))
    home = str(tmp_path)
    sr.setup_repo("alice/thesis", yes=True, run=run, home=home, host="box", out=io.StringIO())
    for argv in run.argvs():
        assert argv[0] in ("ssh-keygen", "git")
        assert "-c" not in argv[:2] and not any(";" in a or "&&" in a for a in argv)


def test_setup_repo_records_the_checkout_for_preflight(tmp_path):
    home = str(tmp_path)
    run = FakeRunner(ok(["ssh-keygen"], side_effect=keygen_side_effect), ok(["git"]))
    path = str(tmp_path / "elsewhere" / "proj")
    sr.setup_repo(
        "alice/thesis", path=path, yes=True, run=run, home=home, host="box", out=io.StringIO()
    )
    with open(sr.record_path(home), encoding="utf-8") as f:
        assert f.read() == path + "\n"
