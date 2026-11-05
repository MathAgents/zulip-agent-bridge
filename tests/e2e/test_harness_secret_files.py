"""A secrets file read without quoting it (#519), offline, with decoy zuliprcs in tmp dirs."""

from __future__ import annotations

import asyncio
import configparser
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from e2e.harness.secret_files import SecretFileError, described, label_of, read_secret
from e2e.harness.zulip_user import TestUser, zulip_client

#: A decoy key, never a real one: it must appear in no message, report or output.
DECOY = "DECOY-SECRET-519"
#: Malformed zuliprcs whose configparser error quotes the decoy's line.
NO_HEADER = f"key={DECOY}\n"
NO_DELIMITER = f"[api]\nemail=t@zulip.example\n{DECOY}\nsite=https://z.example\n"


@pytest.mark.parametrize(
    ("text", "kind"),
    [(NO_HEADER, "MissingSectionHeaderError"), (NO_DELIMITER, "ParsingError")],
)
def test_configparser_quotes_the_line_and_read_secret_does_not(tmp_path, text, kind):
    rc = tmp_path / "admin-e2e" / "secrets" / "bots" / "decoy.zuliprc"
    rc.parent.mkdir(parents=True)
    rc.write_text(text)
    with pytest.raises(configparser.Error) as raw:  # the risk: the line, quoted
        configparser.ConfigParser(interpolation=None).read_string(rc.read_text())
    assert DECOY in str(raw.value) and type(raw.value).__name__ == kind

    def parse() -> str:
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(rc.read_text())
        return config.get("api", "key")

    label = label_of(rc, tmp_path / "admin-e2e")
    with pytest.raises(SecretFileError) as caught:
        read_secret(label, parse)
    assert str(caught.value) == f"secrets/bots/decoy.zuliprc: {kind}"
    assert (caught.value.label, caught.value.kind) == ("secrets/bots/decoy.zuliprc", kind)
    # unchained: no traceback shows the original's message or its frames' arguments
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None
    assert DECOY not in "".join(traceback.format_exception(caught.value))
    assert DECOY not in described(caught.value)


def test_the_test_users_zuliprc_is_named_never_quoted(tmp_path, monkeypatch):
    """The participant rig's and the admin rig's test users (`TestUser`, the admin setup's
    clients): `~/.config/zab/tester.zuliprc`, labelled under `~/.config/zab`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = tmp_path / ".config" / "zab" / "tester.zuliprc"
    rc.parent.mkdir(parents=True)
    rc.write_text(NO_HEADER)
    for opener in (zulip_client, TestUser.from_zuliprc):
        with pytest.raises(SecretFileError, match=r"^tester\.zuliprc: Missing") as caught:
            opener(rc)
        assert DECOY not in "".join(traceback.format_exception(caught.value))
    rc.write_text("[api]\nemail=t@zulip.example\nkey=k\nsite=https://z.example\n")
    user = TestUser.from_zuliprc(rc)  # a well-formed one opens as before
    assert user.client.site == "https://z.example"
    asyncio.run(user.close())


def test_a_missing_file_and_an_outside_path_are_named_too(tmp_path):
    gone = tmp_path / "gone.zuliprc"
    with pytest.raises(SecretFileError) as caught:
        read_secret(label_of(gone, tmp_path), gone.read_text)
    assert str(caught.value) == "gone.zuliprc: FileNotFoundError"
    assert label_of(tmp_path / "x" / "y.zuliprc", tmp_path / "z") == str(tmp_path / "x/y.zuliprc")
    with pytest.raises(SecretFileError) as nested:  # a SecretFileError passes through as it is
        read_secret("outer", read_secret, "inner", int, "not a number")
    assert str(nested.value) == "inner: ValueError"


def test_described_gives_the_type_alone_for_an_empty_message_or_a_parse_error():
    assert described(RuntimeError("exit 1: ssh: no")) == "RuntimeError: exit 1: ssh: no"
    assert described(asyncio.CancelledError()) == "CancelledError"  # no trailing colon
    assert described(KeyboardInterrupt()) == "KeyboardInterrupt"
    unwrapped = configparser.MissingSectionHeaderError("rc", 1, f"key={DECOY}\n")
    assert DECOY in str(unwrapped) and described(unwrapped) == "MissingSectionHeaderError"
    decoded = UnicodeDecodeError("utf-8", b"\xff" + DECOY.encode(), 0, 1, "invalid start byte")
    assert described(decoded) == "UnicodeDecodeError"
    assert described(SecretFileError("provisioner.zuliprc", "ParsingError")) == (
        "SecretFileError: provisioner.zuliprc: ParsingError"
    )


#: A valid secret read before the malformed one: a frame that kept it would show it too.
EARLIER = "DECOY-EARLIER-519"
#: Each wrapped read that runs inside pytest, on a malformed decoy (reviewer on #522).
PATHS = """
from e2e.admin.harness import rig as rigs
from e2e.admin.harness.kept import Laptop
from e2e.admin.harness.zulip_rig import secret_values, zuliprc_email
from e2e.harness.zulip_user import TestUser

RIG = rigs.load()


def test_secret_values():  # A12, and the admin runner's pytest.log sweep
    secret_values(RIG)


def test_provisioner():
    zuliprc_email(RIG.provisioner_zuliprc, RIG.dir)


def test_tester():  # both conftests' `tester`
    TestUser.from_zuliprc(RIG.tester_zuliprc)


def test_laptop():  # A18
    Laptop(RIG.dir / "laptop", RIG).bot_email("codex")
"""


@pytest.mark.parametrize("flags", [[], ["-l"], ["--tb=long", "-l"]])
def test_no_frame_shows_a_secret_even_with_showlocals(tmp_path, flags):
    """Reviewer on #522: with `-l` (an inherited PYTEST_ADDOPTS, say) pytest prints every
    frame's locals; a frame that held the file's text, or the keys read before it, showed
    them. Every read happens below `read_secret`'s unchained raise now."""
    rig = tmp_path / ".config" / "zab" / "admin-e2e"
    (rig / "secrets" / "bots").mkdir(parents=True)
    (rig / "secrets" / "a.zuliprc").write_text(f"[api]\nemail=a@z\nkey={EARLIER}\n")
    for path in (
        rig / "secrets" / "bots" / "z.zuliprc",  # sorted after a.zuliprc
        rig / "provisioner.zuliprc",
        tmp_path / ".config" / "zab" / "tester.zuliprc",
        rig / "laptop" / "out" / "zab-test-c" / "codex.zuliprc",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[api]\nemail=b@z\n{DECOY}\n")  # no `=`: ParsingError
    (tmp_path / "test_paths.py").write_text(PATHS)
    tests = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "HOME": str(tmp_path), "PYTHONPATH": tests}
    for var in ("PYTEST_ADDOPTS", "ZAB_ADMIN_E2E_DIR", "ZAB_E2E_TESTER_ZULIPRC"):
        env.pop(var, None)
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "-q"]
    done = subprocess.run(  # noqa: S603 - an argument list
        [*argv, "-rA", *flags, str(tmp_path / "test_paths.py")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=100, check=False,
    )  # fmt: skip
    output = done.stdout + done.stderr
    assert "4 failed" in output, output[-3000:]
    for label in ("secrets/bots/z.zuliprc", "provisioner.zuliprc", "tester.zuliprc",
                  "out/zab-test-c/codex.zuliprc"):  # fmt: skip
        assert f"SecretFileError: {label}: ParsingError" in output
    assert "DECOY" not in output, output[-3000:]
