"""A malformed zuliprc is named, never quoted (#523).

configparser's errors quote the offending line, which may be `key=…`. `admin_common.load_zuliprc`
says only the file and what failed, and each admin script that loads a zuliprc says that and no
more: in its stdout, its stderr and any traceback. The scripts run as the admin bot runs them, as
subprocesses whose output is captured (`zab.admin.runner`), on decoy files in a tmp dir: never a
real key (CONTRIBUTING.md, rule 2).
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import traceback

import admin_common as ac
import httpx
import move_bots
import push_waker
import pytest
import zulip_provision as zp

ADMIN = pathlib.Path(__file__).resolve().parents[2] / "workshop" / "admin"
DECOY = "DECOY-SECRET-523"  # a decoy, never a real key
SITE = "https://zulip.example.org"
GOOD = f"[api]\nemail=p@example.org\nkey={'K' * 32}\nsite={SITE}\n"
#: A zuliprc with the decoy on the line configparser can't read, and the type of its error.
MALFORMED = {
    "before [api]": (f"key={DECOY}\n[api]\nemail=p@example.org\nsite={SITE}\n".encode(),
                     "MissingSectionHeaderError"),
    "no =": (f"[api]\nemail=p@example.org\nsite={SITE}\nkey {DECOY}\n".encode(), "ParsingError"),
    "no name": (f"[api]\nemail=p@example.org\nsite={SITE}\n=key={DECOY}\n".encode(),
                "ParsingError"),
    "not UTF-8": (f"[api]\nemail=p@example.org\nsite={SITE}\nkey={DECOY}".encode() + b"\xff\n",
                  "UnicodeDecodeError"),
}  # fmt: skip
ROSTER = "slug,first_name,last_name,email\ncarol,Carol,Example,carol@example.org\n"
_HEAD = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
KEY = "ssh-ed25519 " + base64.b64encode(_HEAD + bytes(range(32))).decode()  # test_new_workshop's


@pytest.fixture(params=sorted(MALFORMED))
def bad(request, tmp_path) -> tuple[pathlib.Path, str]:
    """A malformed decoy zuliprc in a tmp dir, and the error's type."""
    data, why = MALFORMED[request.param]
    path = tmp_path / "decoy.zuliprc"
    path.write_bytes(data)
    path.chmod(0o600)
    return path, why


def shown(exc: BaseException) -> str:
    """Everything a traceback of `exc` prints, its chain included."""
    return "".join(traceback.format_exception(exc))


# ------------------------------------------------------------------------------- load_zuliprc


def test_a_malformed_zuliprc_is_named_by_its_type_and_chains_nothing(bad):
    path, why = bad
    with pytest.raises(ValueError) as caught:
        ac.load_zuliprc(str(path))
    exc = caught.value
    assert str(exc) == f"{path}: {why}"
    assert exc.__cause__ is None and exc.__context__ is None and exc.__suppress_context__
    assert DECOY not in shown(exc)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (f"[apl]\nkey={DECOY}\n", "no [api] section"),
        (f"[api]\nemail=p@example.org\nsite={SITE}\n[other]\nkey={DECOY}\n", "no key in [api]"),
        (f"[api]\nkey={DECOY}\nsite={SITE}\n", "no email in [api]"),
        (f"[api]\nkey={DECOY}\nemail=p@example.org\n", "no site in [api]"),
        (f"[api]\nkey={DECOY}\nemail=p@example.org\nsite=http://zulip.example.org\n",
         "the Zulip site must use HTTPS"),
    ],
)  # fmt: skip
def test_a_zuliprc_without_a_part_says_which(tmp_path, text, said):
    path = tmp_path / "decoy.zuliprc"
    path.write_text(text)
    with pytest.raises(ValueError) as caught:
        ac.load_zuliprc(str(path))
    assert str(caught.value) == f"{path}: {said}"
    assert DECOY not in shown(caught.value)


def test_a_good_zuliprc_and_a_missing_one(tmp_path):
    path = tmp_path / "p.zuliprc"
    path.write_text(GOOD.replace(SITE, SITE + "/"))
    assert ac.load_zuliprc(str(path)) == (SITE, "p@example.org", "K" * 32)
    with pytest.raises(FileNotFoundError):  # its message is the path's alone
        ac.load_zuliprc(str(tmp_path / "missing.zuliprc"))


def test_zulip_provisions_own_read_of_a_bots_zuliprc_is_silent(bad, capsys):
    path, _ = bad
    assert zp._zuliprc_email(path) == ""
    assert capsys.readouterr() == ("", "")


# -------------------------------------------------------------------------- the entry points


def run(script: str, *argv: str | pathlib.Path, env: dict[str, str] | None = None):
    """`script` as the admin bot's runner starts one (`-E -s -B -u`, its own environment), its
    stdout and stderr merged as the runner merges them."""
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    done = subprocess.run(  # noqa: S603 - an argument list
        [sys.executable, "-E", "-s", "-B", "-u", str(ADMIN / script), *map(str, argv)],
        env={**base, **(env or {})}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert DECOY not in done.stdout, done.stdout
    assert "Traceback" not in done.stdout, done.stdout
    return done.returncode, done.stdout


def roster(tmp_path: pathlib.Path, text: str = ROSTER) -> pathlib.Path:
    path = tmp_path / "roster.csv"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_move_bots_zuliprc(bad, tmp_path):
    path, why = bad
    code, out = run("move_bots.py", "--roster", roster(tmp_path, "slug,email\n"),
                    "--zuliprc", path, "--to", "me")  # fmt: skip
    assert (code, out) == (2, f"move refused: {path}: {why}\n")


def test_move_bots_a_participant_bots_zuliprc(bad, tmp_path):
    path, why = bad
    keys = tmp_path / "out" / "carol"
    keys.mkdir(parents=True)
    path.rename(keys / f"{zp.BOT_KINDS[0]}.zuliprc")  # the first read
    good = tmp_path / "p.zuliprc"
    good.write_text(GOOD)
    code, out = run("move_bots.py", "--roster", roster(tmp_path), "--zuliprc", good,
                    "--to", "me", "--out-dir", tmp_path / "out")  # fmt: skip
    assert (code, out) == (2, f"move refused: {keys / zp.BOT_KINDS[0]}.zuliprc: {why}\n")


def test_move_bots_provisioner_zuliprc(bad, tmp_path, capsys):
    """Read only once the provisioner is found by ID: a fake realm answers that."""
    path, why = bad
    users = {
        "users/me": {"user_id": 1, "full_name": "Owner", "role": 100},
        "users/8": {"user": {"user_id": 8, "full_name": "zab-provisioner", "is_bot": False,
                             "is_active": True, "role": 400, "email": "p@example.org"}},
    }  # fmt: skip

    def answer(request: httpx.Request) -> httpx.Response:
        found = users.get(request.url.path.removeprefix("/api/v1/"))
        body = {"result": "success", **found} if found else {"result": "error", "code": "X"}
        return httpx.Response(200 if found else 404, content=json.dumps(body).encode())

    good = tmp_path / "p.zuliprc"
    good.write_text(GOOD)
    argv = ["--roster", str(roster(tmp_path, "slug,email\n")), "--zuliprc", str(good),
            "--to", "provisioner", "--provisioner-name", "zab-provisioner",
            "--provisioner-id", "8", "--provisioner-zuliprc", str(path)]  # fmt: skip
    assert move_bots.main(argv, transport=httpx.MockTransport(answer)) == 2
    assert capsys.readouterr() == ("", f"move refused: {path}: {why}\n")


def test_new_workshop(bad, tmp_path):
    path, why = bad
    (tmp_path / "fleet.pub").write_text(KEY + " zab-admin@server\n")
    code, out = run(
        "new_workshop.py", "--plan", "P-9", "--name", "mit-2027", "--region", "us-east-2",
        "--zuliprc", path, "--tag", "workshop=mit-2027", "--scope", "zab-role=test",
        "--instance-type", "m7a.2xlarge", "--github-org", "MathAgents",
        "--refserve-url", "https://files.example.org", "--admin-key", tmp_path / "fleet.pub",
        "--organizer", "3", "--admin", "1", "--ws-root", tmp_path / "ws",
        "--secrets", tmp_path / "secrets", "--public-key", tmp_path / "fleet.pub",
        "--request-dir", tmp_path / "requests", "--result", tmp_path / "result.json",
        "--dry-run",
    )  # fmt: skip
    assert (code, out) == (2, f"new_workshop.py: {path}: {why}\n")


def test_provision_repos(bad, tmp_path):
    path, why = bad
    code, out = run("provision_repos.py", "--roster", roster(tmp_path), "--zuliprc", path,
                    "--dry-run", env={"GH_TOKEN": "not-a-token"})  # fmt: skip
    assert (code, out) == (2, f"Zulip: {path}: {why}\n")


def test_roster_import(bad, tmp_path):
    """An e-mail change of a row on no record is checked against Zulip, with `--zuliprc`."""
    path, why = bad
    upload = tmp_path / "upload.csv"
    upload.write_text(ROSTER.replace("carol@", "carol2@"))
    code, out = run("roster_import.py", "--roster", roster(tmp_path), "--input", upload,
                    "--zuliprc", path, "--dry-run")  # fmt: skip
    assert code == 1
    assert out == (
        f"refused: carol: e-mail change: cannot check the old address: {path}: {why}\n"
        "nothing written\n"
    )


def test_zulip_provision(bad, tmp_path):
    path, why = bad
    code, out = run("zulip_provision.py", "--roster", roster(tmp_path), "--zuliprc", path,
                    "--out-dir", tmp_path / "out", "--dry-run")  # fmt: skip
    assert (code, out) == (1, f"zuliprc: {path}: {why}\n")


# ------------------------------------------------------------------------- the AWS config


def test_push_wakers_aws_config_is_named_never_quoted():
    """push_waker.py's AWS config is no secrets file (`aws_config` writes an ARN and settings),
    but an AWS config may hold keys, and `--check` reads whatever AWS_CONFIG_FILE the unit
    names: a parse error says its type alone (#523)."""
    good = push_waker.aws_config("arn:aws:iam::123456789012:role/zab-waker")
    assert push_waker.role_arn_of(good) == "arn:aws:iam::123456789012:role/zab-waker"
    for text, why in [
        (f"aws_secret_access_key = {DECOY}\n" + good, "MissingSectionHeaderError"),
        (good + f"aws_secret_access_key {DECOY}\n", "ParsingError"),
    ]:
        with pytest.raises(ValueError) as caught:
            push_waker.role_arn_of(text)
        assert str(caught.value) == f"the AWS config doesn't parse: {why}"
        assert caught.value.__context__ is None and DECOY not in shown(caught.value)
