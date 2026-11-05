"""The kept fleet's move to the admin server (docs/ADMIN.md §8.4; #358): staging its state,
joining its VMs over a break-glass key, and moving its bots to the provisioner."""

from __future__ import annotations

import base64
import json
import os
import shlex
import stat
import struct
import subprocess
import sys
import urllib.parse

import httpx
import join_server
import move_bots
import pytest
import stage_server
from admin_common import RunResult, load_zuliprc, read_roster
from zulip_provision import ZulipAdmin, ZulipAdminError

from .member_realm import (
    ADMIN,
    ADMINS,
    EVERYONE,
    MEMBER,
    MEMBERS,
    MODERATOR,
    MODERATORS,
    NOBODY,
    NOT_ADMINISTRATOR,
    OWNER,
    MemberRealm,
)

_HEAD = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
SERVER = "ssh-ed25519 " + base64.b64encode(_HEAD + bytes(range(32))).decode() + " zab-admin@srv"
DREW = "ssh-ed25519 " + base64.b64encode(_HEAD + bytes(range(1, 33))).decode() + " drew@laptop"


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


#: A well-formed key of another type, which parse_public_key accepts.
ECDSA = (
    "ecdsa-sha2-nistp256 "
    + base64.b64encode(
        _string(b"ecdsa-sha2-nistp256") + _string(b"nistp256") + _string(b"\x04" + bytes(64))
    ).decode()
)
ROSTER = (
    "slug,email,ip,region,zulip_user_id\n"
    "alice,a@example.test,192.0.2.7,us-west-2,20\n"
    "bob,b@example.test,192.0.2.8,us-west-2,21\n"
    "carol,c@example.test,,,\n"  # a late joiner, never launched
    "dave,d@example.test,192.0.2.9,us-west-2,23\n"  # torn down: not kept
)


@pytest.fixture
def laptop(tmp_path):
    """Drew's `~/zab-admin`: the Oct 9 roster, out/ with every row's bot keys, keep.txt."""
    (tmp_path / "roster.csv").write_text(ROSTER)
    out = tmp_path / "out"
    for slug in ("alice", "bob", "dave"):
        (out / slug).mkdir(parents=True)
        for kind in ("codex", "claude"):
            (out / slug / f"{kind}.zuliprc").write_text(
                "[api]\n"
                f"email={kind}-{slug}-bot@org.zulipchat.com\n"
                f"key=DECOY{slug}{kind}{'k' * 20}\n"
                "site=https://org.zulipchat.com\n"
            )
    (out / "known_hosts").write_text("192.0.2.7 ssh-ed25519 AAAA\n")
    (tmp_path / "keep.txt").write_text("# kept for a month\nalice\nBob  # upper case is fine\n")
    (tmp_path / "server.pub").write_text(SERVER + "\n")
    (tmp_path / "drew-laptop").write_text("PRIVATE DECOY\n")
    return tmp_path


# ------------------------------------------------------------------------------- staging


def stage_args(laptop, *extra):
    return ["--roster", str(laptop / "roster.csv"), "--keep", str(laptop / "keep.txt"),
            "--out-dir", str(laptop / "out"), "--to", str(laptop / "stage"), *extra]  # fmt: skip


def test_stage_takes_the_kept_rows_their_keys_and_known_hosts(laptop):
    stage = laptop / "stage"
    assert stage_server.main(stage_args(laptop)) == 0
    assert [r["slug"] for r in read_roster(str(stage / "roster.csv"))] == ["alice", "bob"]
    assert read_roster(str(stage / "roster.csv"))[0]["ip"] == "192.0.2.7"  # every column
    files = sorted(str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file())
    assert files == [
        "out/alice/claude.zuliprc",
        "out/alice/codex.zuliprc",
        "out/bob/claude.zuliprc",
        "out/bob/codex.zuliprc",
        "out/known_hosts",
        "roster.csv",
    ]  # nothing of dave's, who is not kept
    for path in stage.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path
    assert stage_server.main(stage_args(laptop)) == 2  # STAGE exists: refused


@pytest.mark.parametrize(
    "keep, error",
    [
        ("alice\neve\n", "not in the roster: eve"),
        ("alice\ncarol\n", "carol: no VM"),
        ("alice\nalice\n", "distinct slugs"),
        ("# nobody\n", "distinct slugs"),
    ],
)
def test_stage_refuses_before_writing_anything(laptop, capsys, keep, error):
    (laptop / "keep.txt").write_text(keep)
    assert stage_server.main(stage_args(laptop)) == 2
    assert error in capsys.readouterr().err and not (laptop / "stage").exists()
    (laptop / "keep.txt").write_text("alice\n")
    (laptop / "out/alice/codex.zuliprc").unlink()
    assert stage_server.main(stage_args(laptop)) == 2 and not (laptop / "stage").exists()
    (laptop / "out/alice/codex.zuliprc").symlink_to(laptop / "out/dave/codex.zuliprc")
    assert stage_server.main(stage_args(laptop)) == 2 and not (laptop / "stage").exists()
    assert "missing regular file" in capsys.readouterr().err


ADMIN_TOML = """\
[[workshop]]
name = "harvard-2026"
roster = "{harvard}"
[[workshop]]
name = "nt-agents-2026"
roster = "{kept}"
[[workshop]]
name = "mit-2027"
roster = "{nowhere}"
"""


@pytest.mark.parametrize(
    "harvard, retired, error",
    [
        ("slug,email\ncarl,c@x.test\n", "", None),
        ("slug,email\nbob,b2@x.test\n", "", "bob is in harvard-2026"),
        ("slug,email\ncarl,c@x.test\n", "alice  # oct-2025\n", "alice: a retired workshop's slug"),
    ],
)
def test_on_the_server_a_slug_another_workshop_has_is_refused(
    laptop, capsys, harvard, retired, error
):
    """§2.1: slugs are unique on the server for its whole life. The second run, on the server,
    reads every other workshop's roster from admin.toml (one not written yet takes nothing) and
    the retired slugs, as `!roster import` does."""
    assert stage_server.main(stage_args(laptop)) == 0
    server = laptop / "server"
    server.mkdir()
    (server / "harvard.csv").write_text(harvard)
    (server / "retired.txt").write_text(retired)
    config = server / "admin.toml"
    config.write_text(ADMIN_TOML.format(harvard=server / "harvard.csv",
                                        kept=server / "kept/roster.csv",
                                        nowhere=server / "none.csv"))  # fmt: skip
    staged = laptop / "stage"
    argv = ["--roster", str(staged / "roster.csv"), "--out-dir", str(staged / "out"),
            "--to", str(server / "kept"), "--config", str(config),
            "--workshop", "nt-agents-2026", "--retired", str(server / "retired.txt")]  # fmt: skip
    if error is None:
        assert stage_server.main(argv) == 0  # every row of the stage, without --keep
        assert [r["slug"] for r in read_roster(str(server / "kept/roster.csv"))] == ["alice", "bob"]
        return
    assert stage_server.main(argv) == 2
    assert error in capsys.readouterr().err and not (server / "kept").exists()


def test_on_the_server_the_flags_go_together(laptop, capsys):
    (laptop / "admin.toml").write_text('[[workshop]]\nname = "other"\nroster = "/nowhere"\n')
    config = ["--config", str(laptop / "admin.toml")]
    with pytest.raises(SystemExit):
        stage_server.main(stage_args(laptop, *config))  # without --workshop
    assert stage_server.main(stage_args(laptop, *config, "--workshop", "typo")) == 2
    assert "not a workshop of" in capsys.readouterr().err


# ------------------------------------------------------------------------------- joining


def run_remote(home, mode: str, key: str = SERVER) -> str:
    """The fixed program join_server.py sends, run here as the VM would run it."""
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    done = subprocess.run(  # noqa: S603 - our own fixed program
        [sys.executable, "-I", "-c", join_server.REMOTE, mode],
        input=key + "\n",
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_the_remote_program_appends_once_whatever_the_comment(tmp_path):
    keys = tmp_path / ".ssh/authorized_keys"
    assert run_remote(tmp_path, "check") == "absent" and not keys.exists()  # a dry run
    assert run_remote(tmp_path, "add") == "added"
    assert keys.read_text() == SERVER + "\n" and stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys.parent.stat().st_mode) == 0o700
    assert run_remote(tmp_path, "add") == "present"
    assert run_remote(tmp_path, "add", SERVER.rsplit(" ", 1)[0] + " renamed") == "present"
    assert keys.read_text() == SERVER + "\n"
    keys.write_text(DREW)  # the break-glass key, without a final newline
    assert run_remote(tmp_path, "check") == "absent"
    assert run_remote(tmp_path, "add") == "added"
    assert keys.read_text() == f"{DREW}\n{SERVER}\n"
    keys.write_text(f"no-pty {SERVER}\n")  # restricted: not the key the server needs
    assert run_remote(tmp_path, "add") == "added"


def test_the_remote_program_removes_every_line_that_holds_the_servers_key(tmp_path):
    """A server lost or compromised (§1.7), and the cutover's fallback (ADMIN §8.4 at
    v0.2.15): every line whose key fields are the key, whatever options come before them (a
    restricted line still lets the server in; a quoted option may hold a blank) or comment
    after them, and nothing else: not a line that only mentions the key in an option or a
    comment. The count is printed."""
    keys = tmp_path / ".ssh/authorized_keys"
    assert run_remote(tmp_path, "remove") == "removed 0" and not keys.exists()
    material = SERVER.rsplit(" ", 1)[0]
    lines = [
        DREW,
        SERVER,
        f"no-pty {SERVER}",
        f'from="10.0.0.0/8,192.0.2.1",command="echo hi" {material} another comment',
        f"{material} zab-admin-nt-agents-2026",
        f'from="10.0.0.0/8, 192.0.2.1",no-pty {material}',  # a blank inside the quotes
        f'command="echo \\\\" x" {material}',  # `\` then `\"`: OpenSSH's escaping (Codex)
        f'command="say \\"hi\\" {material} now" {DREW}',  # escaped quotes, around Drew's key
        "# a comment",
        f"# {SERVER}",  # commented out: no key, and left alone
        f'command="logger replaced {material} by" {DREW}',  # Drew's key, which only mentions it
        f'command="unterminated {SERVER}',  # no key at all for sshd
        ECDSA,
    ]
    keys.parent.mkdir(mode=0o700)
    keys.write_text("\n".join(lines))
    keys.chmod(0o600)
    assert run_remote(tmp_path, "count") == "held 6"  # the dry run
    assert keys.read_text() == "\n".join(lines)
    assert run_remote(tmp_path, "remove") == "removed 6"
    kept = "\n".join([DREW, lines[7], "# a comment", f"# {SERVER}", lines[10], lines[11], ECDSA])
    assert keys.read_text() == kept
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert run_remote(tmp_path, "remove") == "removed 0"  # idempotent
    assert run_remote(tmp_path, "count") == "held 0"
    assert keys.read_text() == kept


def test_the_remote_program_splits_lines_as_sshd_does(tmp_path):
    """Review of #365: sshd splits `authorized_keys` at `\\n` alone. A lone `\\r` inside an
    option, or `\\x0c` and `\\x85` in a comment, are part of the line: the server's key behind
    one still goes, and Drew's line with one comes back byte for byte (split, it would lock him
    out)."""
    keys = tmp_path / ".ssh/authorized_keys"
    material = SERVER.rsplit(" ", 1)[0]
    kept = [
        f'environment="X=y\r z" {DREW}',
        f"{DREW} a\x0cb\x85c",
    ]
    gone = [f'environment="X=y\r z" {material} srv', SERVER]
    keys.parent.mkdir(mode=0o700)
    keys.write_bytes("\n".join([kept[0], gone[0], kept[1], gone[1]]).encode() + b"\n")
    assert run_remote(tmp_path, "count") == "held 2"
    assert run_remote(tmp_path, "remove") == "removed 2"
    assert keys.read_bytes() == ("\n".join(kept) + "\n").encode()
    assert run_remote(tmp_path, "check") == "absent"
    assert run_remote(tmp_path, "add") == "added"  # appended after the kept bytes
    assert keys.read_bytes() == ("\n".join([*kept, SERVER]) + "\n").encode()


def test_whitespace_inside_the_base64_does_not_hide_the_key(tmp_path):
    """Review of #365: sshd splits a line's fields at blanks and tabs only, and `b64_pton`
    skips any other whitespace inside the base64, so `ssh-ed25519 AAAAC3Nza\\vC1lZ…` still
    lets the server in. The comparison drops that whitespace too."""
    keys = tmp_path / ".ssh/authorized_keys"
    kind, blob = SERVER.split()[:2]
    mine = DREW.split()[1]
    hidden = [
        f"{kind} {blob[:9]}\x0b{blob[9:]} srv",  # the reviewer's case
        f"{kind} {blob[:5]}\x0c{blob[5:12]}\r{blob[12:]}",
        f"no-pty {kind} {blob[:20]}\x0b\x0c{blob[20:]}\r",
    ]
    kept = [f"{kind} {mine[:9]}\x0b{mine[9:]} drew", "# no key"]
    keys.parent.mkdir(mode=0o700)
    keys.write_bytes("\n".join([*hidden, *kept]).encode() + b"\n")
    assert run_remote(tmp_path, "count") == "held 3"
    assert run_remote(tmp_path, "remove") == "removed 3"
    assert keys.read_bytes() == ("\n".join(kept) + "\n").encode()


class Fake:
    """The VMs, answering what the remote program would print, by address; and `ssh-keygen -y`,
    printing `own` (the identity's public key), or failing as for an encrypted key."""

    def __init__(self, answers, own=None):
        self.answers, self.own, self.calls, self.keygen = answers, own, [], []

    def __call__(self, argv, *, timeout_s=120.0, env=None, stdin=None):
        if argv[0] == "ssh-keygen":
            self.keygen.append(argv)
            return RunResult(0, self.own + "\n", "") if self.own else RunResult(255, "", "no")
        self.calls.append((argv, stdin))
        return self.answers[argv[argv.index("--") - 1].split("@")[1]]


def join_args(laptop, *extra):
    roster = laptop / "roster.csv"
    return ["--roster", str(roster), "--identity", str(laptop / "drew-laptop"),
            "--server-key", str(laptop / "server.pub"),
            "--known-hosts", str(laptop / "out/known_hosts"), *extra]  # fmt: skip


def test_join_appends_over_the_break_glass_key_alone(laptop, capsys, monkeypatch):
    monkeypatch.setenv("ZAB_SSH_IDENTITY", "/elsewhere/key")  # the e2e's: never this one
    runner = Fake({
        "192.0.2.7": RunResult(0, "added\n", ""),
        "192.0.2.8": RunResult(0, "present\n", ""),
        "192.0.2.9": RunResult(255, "", "ubuntu@192.0.2.9: Permission denied (publickey).\n"),
    })  # fmt: skip
    assert join_server.main(join_args(laptop), runner=runner) == 1
    out = capsys.readouterr().out
    assert "ok: added" in out and "ok: already there" in out and "skip: no VM" in out
    assert "FAILED: ubuntu@192.0.2.9: Permission denied (publickey)." in out
    assert "3 of 4 ok, 1 FAILED" in out
    # #435: "the key", whichever --server-key names (the cutover's O5 added the dedicated key
    # with it too, ADMIN §8.4 at v0.2.15)
    assert out.startswith(f"add the key {join_server.fingerprint(SERVER)}\n")
    argv, stdin = next(call for call in runner.calls if "ubuntu@192.0.2.7" in call[0])
    assert stdin == SERVER + "\n"  # the key goes on stdin, never into the command
    assert argv[:7] == ["ssh", "-F", "/dev/null", "-i", str(laptop / "drew-laptop"), "-o",
                        "IdentitiesOnly=yes"]  # fmt: skip
    assert "/elsewhere/key" not in argv
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no",
                   f'UserKnownHostsFile="{laptop / "out/known_hosts"}"'):  # fmt: skip
        assert option in argv
    assert shlex.split(argv[-1]) == ["python3", "-I", "-c", join_server.REMOTE, "add"]
    assert argv[-3:-1] == ["ubuntu@192.0.2.7", "--"]


def test_join_dry_run_only_checks(laptop, capsys):
    runner = Fake({ip: RunResult(0, "absent\n", "") for ip in ("192.0.2.7", "192.0.2.8")})
    assert join_server.main(join_args(laptop, "--keep", str(laptop / "keep.txt"), "--dry-run"),
                            runner=runner) == 0  # fmt: skip
    assert capsys.readouterr().out.count("would add") == 2
    assert all(shlex.split(argv[-1])[-1] == "check" for argv, _ in runner.calls)


def test_join_remove_takes_the_key_off_the_kept_vms(laptop, capsys):
    keep = ["--keep", str(laptop / "keep.txt")]
    runner = Fake({"192.0.2.7": RunResult(0, "held 2\n", ""),
                   "192.0.2.8": RunResult(0, "held 0\n", "")}, own=DREW)  # fmt: skip
    assert join_server.main(join_args(laptop, *keep, "--remove", "--dry-run"), runner=runner) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"DRY RUN: remove the key {join_server.fingerprint(SERVER)}\n")
    assert "would remove 2" in out and "ok: not there" in out
    assert all(shlex.split(argv[-1])[-1] == "count" for argv, _ in runner.calls)
    assert runner.keygen == [["ssh-keygen", "-y", "-P", "", "-f", str(laptop / "drew-laptop")]]
    runner = Fake({"192.0.2.7": RunResult(0, "removed 2\n", ""),
                   "192.0.2.8": RunResult(0, "removed 0\n", "")}, own=DREW)  # fmt: skip
    assert join_server.main(join_args(laptop, *keep, "--remove"), runner=runner) == 0
    assert "ok: removed 2" in capsys.readouterr().out
    assert all(shlex.split(argv[-1])[-1] == "remove" for argv, _ in runner.calls)
    for word in ("added\n", "removed\n", "removed two\n", "held 1\n"):  # not what was asked
        runner = Fake({"192.0.2.7": RunResult(0, word, "")}, own=DREW)
        argv = join_args(laptop, "--only", "alice", "--remove")
        assert join_server.main(argv, runner=runner) == 1, word


@pytest.mark.parametrize("source", ["derived", "pub", "both"])
def test_join_remove_never_takes_off_the_key_it_connects_with(laptop, capsys, source):
    """The identity's public half comes from `ssh-keygen -y`, or from its `.pub` when the key
    is encrypted; that the `.pub` is missing doesn't lift the guard."""
    mine = SERVER.rsplit(" ", 1)[0] + " drew@laptop"
    if source in ("pub", "both"):
        (laptop / "drew-laptop.pub").write_text(mine + "\n")
    runner = Fake({}, own=mine if source in ("derived", "both") else None)
    assert join_server.main(join_args(laptop, "--remove"), runner=runner) == 2
    assert "the key this run connects with" in capsys.readouterr().err and not runner.calls
    runner = Fake({ip: RunResult(0, "present\n", "") for ip in ("192.0.2.7", "192.0.2.8")})
    assert join_server.main(join_args(laptop, "--only", "alice"), runner=runner) == 0  # adding


def test_join_remove_refuses_when_the_identity_can_not_be_read(laptop, capsys):
    runner = Fake({})  # an encrypted key (ssh-keygen -y fails) and no .pub
    assert join_server.main(join_args(laptop, "--remove"), runner=runner) == 2
    assert "public key can't be read" in capsys.readouterr().err and not runner.calls


@pytest.mark.parametrize(
    "answer, shown",
    [
        (RunResult(0, "", ""), "exit 0"),
        (RunResult(0, "added\nadded\n", ""), "added"),
        (RunResult(0, "maybe\n", ""), "maybe"),
        (RunResult(-1, "", "timed out after 90 s: ssh"), "timed out after 90 s: ssh"),
        (
            RunResult(255, "", "@@@@@@@@@@\n@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\n"
                      "@@@@@@@@@@\nHost key verification failed.\n"),
            "the VM's host key is not the pinned one",
        ),
        (
            RunResult(255, "", "@@@@@@@@@@\n@ WARNING: UNPROTECTED PRIVATE KEY FILE! @\n"
                      "@@@@@@@@@@\nPermissions 0644 for 'k' are too open.\n"),
            "the identity file's permissions are too open: chmod 600 it",
        ),
        (RunResult(255, "", "@@@@@@@@\nsomething else\n"), "something else"),
    ],
)  # fmt: skip
def test_join_counts_anything_unexpected_as_failed_and_says_why(laptop, capsys, answer, shown):
    runner = Fake({"192.0.2.7": answer})
    assert join_server.main(join_args(laptop, "--only", "alice"), runner=runner) == 1
    out = capsys.readouterr().out
    assert f"FAILED: {shown}" in out and "FAILED: @" not in out


@pytest.mark.parametrize(
    "change, error",
    [
        (("drew-laptop", None), "--identity"),
        (("server.pub", ECDSA + " x\n"), "must be ssh-ed25519"),  # well-formed, another type
        (("server.pub", f'command="x" {SERVER}\n'), "--server-key"),
        (("server.pub", f"{SERVER}\n{DREW}\n"), "--server-key"),
    ],
)
def test_join_refusals(laptop, capsys, change, error):
    name, text = change
    if text is None:
        (laptop / name).unlink()
    else:
        (laptop / name).write_text(text)
    runner = Fake({})
    assert join_server.main(join_args(laptop), runner=runner) == 2
    assert error in capsys.readouterr().err and not runner.calls
    pub = join_args(laptop)
    pub[pub.index("--identity") + 1] = str(laptop / "server.pub")
    assert join_server.main(pub, runner=runner) == 2  # a .pub is not a private key


# ------------------------------------------------------------------------------- bot owners


class Realm:
    """Zulip's side of the bot endpoints move_bots.py uses (zerver/views/users.py)."""

    def __init__(self):
        self.users = {
            7: {"user_id": 7, "full_name": "Drew", "email": "drew@example.test", "is_bot": False,
                "role": 100},
            8: {"user_id": 8, "full_name": "zab-provisioner", "email": "prov@example.test",
                "is_bot": False, "role": 400},
            9: {"user_id": 9, "full_name": "Gone", "email": "gone@example.test", "is_bot": False,
                "is_active": False},
            20: {"user_id": 20, "full_name": "Alice", "email": "a@example.test", "is_bot": False},
        }  # fmt: skip
        for n, slug in enumerate(("alice", "bob", "dave")):
            for m, kind in enumerate(("codex", "claude")):
                uid = 100 + 10 * n + m
                self.users[uid] = {
                    "user_id": uid,
                    "full_name": f"{kind.capitalize()} · {slug}",
                    "email": f"{kind}-{slug}-bot@org.zulipchat.com",
                    "is_bot": True,
                    "bot_owner_id": 7,
                }
        self.me, self.patches, self.stuck, self.admins = 7, [], False, set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urllib.parse.unquote(request.url.path.removeprefix("/api/v1/"))
        if request.method == "GET" and path == "users/me":
            return self.ok(self.users[self.me])
        if request.method == "GET" and path == "bots":
            mine = [u for u in self.users.values() if u.get("bot_owner_id") == self.me]
            bots = [{"username": u["email"], "full_name": u["full_name"], "api_key": "K" * 32}
                    for u in mine]  # fmt: skip
            return self.ok({"bots": bots})
        if request.method == "GET" and path.startswith("users/"):
            who = path.removeprefix("users/")
            found = [u for u in self.users.values() if who in (str(u["user_id"]), u["email"])]
            if not found:
                return httpx.Response(400, json={"result": "error", "msg": "No such user"})
            return self.ok({"user": {"is_active": True, **found[0]}})
        if request.method == "PATCH" and path.startswith("bots/"):
            bot = self.users[int(path.removeprefix("bots/"))]
            form = urllib.parse.parse_qs(request.content.decode())
            owner = json.loads(form["bot_owner_id"][0])  # Json[int], as the web app sends it
            if bot.get("bot_owner_id") != self.me and self.me not in self.admins:
                return httpx.Response(
                    400, json={"result": "error", "msg": "Insufficient permission"}
                )
            if self.users[self.me].get("role", MEMBER) > ADMIN:  # Zulip Cloud's rule (#758)
                return httpx.Response(400, json=NOT_ADMINISTRATOR)
            self.patches.append((bot["user_id"], owner))
            if not self.stuck:
                bot["bot_owner_id"] = owner
            return self.ok({"bot_owner": self.users[owner]["email"]})
        return httpx.Response(404, json={"result": "error", "msg": f"{request.method} {path}"})

    @staticmethod
    def ok(data):
        return httpx.Response(200, json={"result": "success", **data})


def move_args(laptop, *extra):
    for name, email in (("drew", "drew@example.test"), ("provisioner", "prov@example.test")):
        (laptop / f"{name}.zuliprc").write_text(
            f"[api]\nemail={email}\nkey=" + "d" * 32 + "\nsite=https://org.zulipchat.com\n"
        )
    return ["--roster", str(laptop / "roster.csv"), "--zuliprc", str(laptop / "drew.zuliprc"),
            "--out-dir", str(laptop / "out"), *extra]  # fmt: skip


TO_PROVISIONER = ("--to", "provisioner", "--provisioner-name", "zab-provisioner",
                  "--provisioner-id", "8", "--provisioner-zuliprc")  # fmt: skip


def test_move_bots_is_a_dry_run_until_apply_and_reads_each_owner_back(laptop, capsys):
    realm = Realm()
    transport = httpx.MockTransport(realm.handler)
    kept = [
        "--keep",
        str(laptop / "keep.txt"),
        *TO_PROVISIONER,
        str(laptop / "provisioner.zuliprc"),
    ]
    assert move_bots.main(move_args(laptop, *kept), transport=transport) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "to zab-provisioner <prov@example.test> (8, member)" in out
    assert out.count("would move from user 7") == 4 and not realm.patches
    assert move_bots.main(move_args(laptop, *kept, "--apply"), transport=transport) == 0
    out = capsys.readouterr().out
    assert sorted(realm.patches) == [(100, 8), (101, 8), (110, 8), (111, 8)]
    assert out.count("ok: moved from user 7") == 4 and "DRY RUN" not in out
    assert "2 participant bots still owned by Drew: Claude · dave, Codex · dave" in out
    assert "K" * 32 not in out and "DECOY" not in out  # no key is ever printed
    assert move_bots.main(move_args(laptop, *kept, "--apply"), transport=transport) == 0
    assert capsys.readouterr().out.count("ok: owned already") == 4 and len(realm.patches) == 4


def test_move_bots_gives_them_back_to_the_caller(laptop, capsys):
    """The fallback (the cutover's step 2, ADMIN §8.4 at v0.2.15): Drew's own key, `--to me`. An
    organization administrator may move a bot it doesn't own (`access_bot_by_id`)."""
    realm = Realm()
    realm.admins = {7}
    for uid in (100, 101):
        realm.users[uid]["bot_owner_id"] = 8
    argv = move_args(laptop, "--only", "alice", "--to", "me", "--apply")
    assert move_bots.main(argv, transport=httpx.MockTransport(realm.handler)) == 0
    assert sorted(realm.patches) == [(100, 7), (101, 7)]
    assert "to Drew <drew@example.test> (7, organization owner)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "uid, provisioner, error",
    [
        (9, "gone@example.test", "not an active human"),
        (110, "codex-bob-bot@org.zulipchat.com", "not an active human"),  # a bot
        (99, "nobody@example.test", "No such user"),
        (20, "a@example.test", "a participant of this roster"),  # alice's user ID and address
        (31, "c@example.test", "a participant of this roster"),  # by e-mail only: carol, no ID
        (21, "b-other@example.test", "a participant of this roster"),  # by user ID only: bob's
        (30, "tester@example.test", "user 30 is named 'zab-tester' (30), not 'zab-provisioner'"),
        (32, "imposter@example.test", "user 32 is named 'zab-provisioner2' (32), not"),
        (8, "imposter@example.test", "which is not the address of user 8: refused"),
    ],
)
def test_move_bots_refuses_a_new_owner_that_must_not_get_the_keys(
    laptop, capsys, uid, provisioner, error
):
    """The target is the user of `--provisioner-id` (#396). A participant matched by e-mail
    alone (a row without `zulip_user_id`) or by user ID alone (another address) is refused, and
    so is anyone but the provisioner the runbook names, such as the rig's test provisioner. A
    `--provisioner-zuliprc` that names someone else is refused where the key sees the address,
    as an owner's does."""
    realm = Realm()
    for who, email, name in ((31, "c@example.test", "zab-provisioner"),
                             (21, "b-other@example.test", "zab-provisioner"),
                             (30, "tester@example.test", "zab-tester"),
                             (32, "imposter@example.test", "zab-provisioner2")):  # fmt: skip
        realm.users[who] = {"user_id": who, "full_name": name, "email": email, "is_bot": False,
                            "role": 400}  # fmt: skip
    to = ("--to", "provisioner", "--provisioner-name", "zab-provisioner", "--provisioner-id",
          str(uid), "--provisioner-zuliprc", str(laptop / "p.zuliprc"))  # fmt: skip
    argv = move_args(laptop, "--only", "alice", *to)
    (laptop / "p.zuliprc").write_text(
        f"[api]\nemail={provisioner}\nkey=" + "p" * 32 + "\nsite=https://org.zulipchat.com\n"
    )
    code = move_bots.main([*argv, "--apply"], transport=httpx.MockTransport(realm.handler))
    assert code == 2 and error in capsys.readouterr().err and not realm.patches


@pytest.mark.parametrize("missing", ["--provisioner-name", "--provisioner-id"])
def test_move_bots_needs_the_provisioners_name_and_id(laptop, capsys, missing):
    given = {"--provisioner-name": "zab-provisioner", "--provisioner-id": "8"}
    del given[missing]
    zuliprc = str(laptop / "provisioner.zuliprc")
    argv = move_args(laptop, "--only", "alice", "--to", "provisioner", "--provisioner-zuliprc",
                     zuliprc, *sum(given.items(), ()), "--apply")  # fmt: skip
    realm = Realm()
    assert move_bots.main(argv, transport=httpx.MockTransport(realm.handler)) == 2
    assert missing in capsys.readouterr().err and not realm.patches


@pytest.mark.parametrize("role, moved", [(100, True), (200, False), (400, False)])
def test_a_participant_takes_the_bots_only_as_an_organization_owner(laptop, capsys, role, moved):
    """Drew is a kept participant (`andrew-sutherland`) and an organization owner: the fallback
    gives the bots back to him (its step 2, ADMIN §8.4 at v0.2.15). Any other participant stays
    refused, an administrator too: only an owner can manage every bot already."""
    with open(laptop / "roster.csv", "a") as f:
        f.write("andrew-sutherland,drew@example.test,192.0.2.10,us-west-2,7\n")
    realm = Realm()
    realm.admins, realm.users[7]["role"] = {7}, role
    for uid in (100, 101):
        realm.users[uid]["bot_owner_id"] = 8
    argv = move_args(laptop, "--only", "alice", "--to", "me")
    code = move_bots.main([*argv, "--apply"], transport=httpx.MockTransport(realm.handler))
    if moved:
        assert code == 0 and sorted(realm.patches) == [(100, 7), (101, 7)]
        assert "to Drew <drew@example.test> (7, organization owner)" in capsys.readouterr().out
    else:
        assert code == 2 and not realm.patches
        assert "a participant of this roster, and no owner" in capsys.readouterr().err


def test_move_bots_refuses_the_caller_when_it_is_a_participant(laptop, capsys):
    realm = Realm()
    realm.me = 20  # alice's own key, which owns no bot here
    realm.users[20]["role"] = 400
    argv = move_args(laptop, "--only", "bob", "--to", "me", "--apply")
    assert move_bots.main(argv, transport=httpx.MockTransport(realm.handler)) == 2
    assert "a participant of this roster" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # no other target than these two
        move_bots.main(move_args(laptop, "--to", "prov@example.test"))


def test_move_bots_moves_only_this_rows_participant_bots(laptop, capsys):
    """A zuliprc that names a human, or another row's bot, is refused for its row; a bot the
    caller may not move fails; a deactivated one is left alone; a missing key file refuses all."""
    realm = Realm()
    (laptop / "out/alice/codex.zuliprc").write_text(
        "[api]\nemail=a@example.test\nkey=" + "x" * 32 + "\nsite=https://org.zulipchat.com\n"
    )
    (laptop / "out/alice/claude.zuliprc").write_text(
        "[api]\nemail=claude-dave-bot@org.zulipchat.com\nkey=" + "x" * 32
        + "\nsite=https://org.zulipchat.com\n"
    )  # fmt: skip
    realm.users[110]["bot_owner_id"] = 9  # bob's codex: someone else's, and the caller no admin
    realm.users[111]["is_active"] = False
    argv = move_args(laptop, "--only", "alice", "--only", "bob", *TO_PROVISIONER,
                     str(laptop / "provisioner.zuliprc"), "--apply")  # fmt: skip
    assert move_bots.main(argv, transport=httpx.MockTransport(realm.handler)) == 1
    out = capsys.readouterr().out
    assert "FAILED: a@example.test is not a bot" in out
    assert "is named 'Claude · dave', not 'Claude · alice': refused" in out
    assert "FAILED: Zulip" in out and "Insufficient permission" in out
    assert "deactivated: left alone" in out and not realm.patches
    os.unlink(laptop / "out/bob/claude.zuliprc")
    assert move_bots.main(argv, transport=httpx.MockTransport(realm.handler)) == 2


def test_move_bots_reports_an_owner_that_did_not_change(laptop, capsys):
    realm = Realm()
    realm.stuck = True  # the PATCH answers success, and the owner stays
    argv = move_args(laptop, "--only", "alice", *TO_PROVISIONER,
                     str(laptop / "provisioner.zuliprc"), "--apply")  # fmt: skip
    assert move_bots.main(argv, transport=httpx.MockTransport(realm.handler)) == 1
    assert capsys.readouterr().out.count("FAILED: the owner reads 7 after the move") == 2


def test_move_bots_says_when_it_cannot_list_what_is_left(laptop, capsys):
    """The moves are done by then: a failed `GET /bots` is reported, not a refusal."""
    realm = Realm()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/bots"):
            return httpx.Response(500, json={"result": "error", "msg": "down"})
        return realm.handler(request)

    argv = move_args(laptop, "--only", "alice", *TO_PROVISIONER,
                     str(laptop / "provisioner.zuliprc"), "--apply")  # fmt: skip
    assert move_bots.main(argv, transport=httpx.MockTransport(handler)) == 0
    out = capsys.readouterr().out
    assert "could not list the bots Drew owns" in out and out.count("ok: moved") == 2


def member_realm(laptop) -> MemberRealm:
    """The rig on Oct 15 (#396): `zab-tester2` (41), a Member, owns alice's bots, as Drew owns
    the kept fleet's; the provisioner (8) is a Member too. Every human's address is visible to
    admins and moderators only, as it was then. Their zuliprcs are in `laptop`."""
    realm = MemberRealm()
    realm.human(7, "Drew", "drew@example.test", role=OWNER)
    realm.human(8, "zab-provisioner", "prov@example.test")
    realm.human(9, "Gone", "gone@example.test", is_active=False)
    realm.human(30, "zab-tester", "tester@example.test")
    realm.human(41, "zab-tester2", "tester2@example.test")
    for uid, kind in ((100, "codex"), (101, "claude")):
        realm.bot(uid, f"{kind.capitalize()} · alice", f"{kind}-alice-bot@org.zulipchat.com", 41)
    for name, email in (("tester2", "tester2@example.test"), ("provisioner", "prov@example.test")):
        (laptop / f"{name}.zuliprc").write_text(
            f"[api]\nemail={email}\nkey=" + "m" * 32 + "\nsite=https://org.zulipchat.com\n"
        )
    return realm


def member_move(laptop, *extra) -> list[str]:
    return ["--roster", str(laptop / "roster.csv"), "--zuliprc", str(laptop / "tester2.zuliprc"),
            "--out-dir", str(laptop / "out"), "--only", "alice", "--to", "provisioner",
            "--provisioner-name", "zab-provisioner", *extra]  # fmt: skip


def test_a_key_that_cannot_read_the_address_finds_the_provisioner_by_id(laptop, capsys):
    """#396: a Member's key couldn't read the provisioner's address, whose visibility excluded
    members, so the lookup by its zuliprc's e-mail answered "No such user". By its ID it is
    found, and the zuliprc's e-mail, which the key can't check, is said to be unchecked. Since
    #758 the key that moves is an administrator's, which can't read an address visible to
    nobody either."""
    realm = member_realm(laptop)
    realm.users[41]["role"] = ADMIN
    realm.users[8]["visibility"] = NOBODY
    site, email, key = load_zuliprc(str(laptop / "tester2.zuliprc"))
    admin = ZulipAdmin(site, email, key, transport=realm.transport())
    with pytest.raises(ZulipAdminError, match=r"BAD_REQUEST \(HTTP 400\): No such user"):
        admin.user_by_email("prov@example.test")  # the old lookup
    assert admin.user(8)["email"] == "user8@org.zulipchat.com"  # a dummy
    assert admin.user(8)["delivery_email"] is None
    admin.close()
    realm.requests.clear()
    zuliprc = ("--provisioner-zuliprc", str(laptop / "provisioner.zuliprc"))
    assert move_bots.main(member_move(laptop, "--provisioner-id", "8", *zuliprc),
                          transport=realm.transport()) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert (
        "DRY RUN: as zab-tester2 (41), to zab-provisioner <address hidden from this key> "
        "(8, member)\n--provisioner-zuliprc: its e-mail is not checked, since this key can't "
        "see the user's address"
    ) in out
    assert out.count("would move from user 41") == 2 and "ADMINISTRATOR" not in out
    assert move_bots.main(member_move(laptop, "--provisioner-id", "8", "--apply"),
                          transport=realm.transport()) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert out.count("ok: moved from user 41") == 2 and "--provisioner-zuliprc" not in out
    assert {realm.users[bot]["bot_owner_id"] for bot in (100, 101)} == {8}
    assert "0 participant bots still owned by zab-tester2" in out
    assert move_bots.main(member_move(laptop, "--provisioner-id", "8"),
                          transport=realm.transport()) == 0  # fmt: skip
    assert capsys.readouterr().out.count("ok: owned already") == 2
    assert not realm.asked("users/prov@example.test") and realm.asked("users/8")


#: What move_bots.py says of a key that isn't an administrator's (#758).
NOT_ADMIN_LINE = (
    "NOT AN ADMINISTRATOR'S KEY: zab-tester2 (41) is a {role}. Zulip Cloud lets only an "
    "organization administrator change a bot's owner, not even the bot's owner (#758): use an "
    "administrator's or an organization owner's key"
)


@pytest.mark.parametrize("role, said", [(MEMBER, "member"), (MODERATOR, "moderator")])
def test_a_key_that_is_no_administrators_moves_nothing_and_says_why(laptop, capsys, role, said):
    """#758: Zulip Cloud refuses a bot's owner change by a key that isn't an administrator's,
    the bot's owner's too, as zab-tester2's here, and the rig's test users are a Member and a
    Moderator. The dry run says so and fails each bot that would move; `--apply` asks, and
    each refusal fails its row with the cause, not Zulip's bare error."""
    realm = member_realm(laptop)
    realm.users[41]["role"] = role
    zuliprc = ("--provisioner-zuliprc", str(laptop / "provisioner.zuliprc"))
    assert move_bots.main(member_move(laptop, "--provisioner-id", "8", *zuliprc),
                          transport=realm.transport()) == 1  # fmt: skip
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN: as zab-tester2 (41), to zab-provisioner <")
    assert NOT_ADMIN_LINE.format(role=said) in out.splitlines()
    assert out.count("FAILED: would move from user 41, but needs an administrator's key") == 2
    assert not [r for r in realm.requests if r[1] != "GET"]
    assert move_bots.main(member_move(laptop, "--provisioner-id", "8", "--apply"),
                          transport=realm.transport()) == 1  # fmt: skip
    out = capsys.readouterr().out
    assert NOT_ADMIN_LINE.format(role=said) in out.splitlines()
    refused = (
        "FAILED: not moved: Zulip Cloud lets only an organization administrator change a bot's "
        "owner, not even the bot's owner (#758); Zulip UNAUTHORIZED_PRINCIPAL (HTTP 400): Must be "
        "an organization administrator"
    )
    assert out.count(refused) == 2, out
    assert [r[1:] for r in realm.requests if r[1] != "GET"] == [("PATCH", "bots/100"),
                                                                ("PATCH", "bots/101")]  # fmt: skip
    assert {realm.users[bot]["bot_owner_id"] for bot in (100, 101)} == {41}


def test_a_dry_run_with_nothing_to_move_passes_with_any_key(laptop, capsys):
    """A18 on the rig (#758): the bots are the provisioner's already, so `--apply` would change
    nothing, and the dry run with a Member's key passes, saying the key isn't an
    administrator's. A deactivated bot is left alone before the key's role counts, even one
    that isn't the provisioner's yet, which pins the order of `move`'s checks."""
    realm = member_realm(laptop)
    realm.users[100]["bot_owner_id"] = 8
    realm.users[101]["is_active"] = False  # still zab-tester2's
    assert move_bots.main(member_move(laptop, "--provisioner-id", "8"),
                          transport=realm.transport()) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert NOT_ADMIN_LINE.format(role="member") in out.splitlines()
    rows = {line.split()[1]: line.split(None, 2)[2].strip() for line in out.splitlines()
            if line.startswith("alice ")}  # fmt: skip
    assert rows == {"codex": "ok: owned already", "claude": "deactivated: left alone"}
    assert "FAILED" not in out


@pytest.mark.parametrize(
    "role, moved", [(OWNER, True), (ADMIN, True), (MODERATOR, False), (MEMBER, False)]
)
def test_the_fake_realm_changes_a_bots_owner_as_zulip_cloud_does(laptop, role, moved):
    """Zulip Cloud since Nov 2 (#758): only an administrator or an owner changes a bot's owner,
    and the bot's owner who is neither gets the exact refusal seen live; the owner still
    deactivates their bot (`DELETE /bots/{id}`)."""
    realm = member_realm(laptop)
    realm.users[41]["role"] = role
    auth = base64.b64encode(b"tester2@example.test:" + b"m" * 32).decode()
    with httpx.Client(transport=realm.transport(), base_url="https://org.zulipchat.com",
                      headers={"Authorization": f"Basic {auth}"}) as client:  # fmt: skip
        answer = client.patch("/api/v1/bots/100", data={"bot_owner_id": "8"})
        if moved:
            assert answer.status_code == 200 and answer.json()["result"] == "success"
        else:
            assert answer.status_code == 400 and answer.json() == {
                "result": "error",
                "msg": "Must be an organization administrator",
                "code": "UNAUTHORIZED_PRINCIPAL",
            }
        assert realm.users[100]["bot_owner_id"] == (8 if moved else 41)
        assert client.delete("/api/v1/bots/101").status_code == 200
    assert realm.users[101]["is_active"] is False


@pytest.mark.parametrize(
    "uid, error",
    [
        (9, "not an active human"),
        (100, "not an active human"),  # alice's Codex bot
        (99, "No such user"),
        (30, "user 30 is named 'zab-tester' (30), not 'zab-provisioner': refused"),
    ],
)
def test_a_member_key_refuses_by_id_anyone_but_the_provisioner(laptop, capsys, uid, error):
    realm = member_realm(laptop)
    argv = member_move(laptop, "--provisioner-id", str(uid), "--apply")
    assert move_bots.main(argv, transport=realm.transport()) == 2
    assert error in capsys.readouterr().err
    assert {realm.users[bot]["bot_owner_id"] for bot in (100, 101)} == {41}


@pytest.mark.parametrize(
    "visibility, said",
    [
        (MODERATORS, "--provisioner-zuliprc: its e-mail is this user's"),
        (ADMINS, "--provisioner-zuliprc: its e-mail is not checked"),
    ],
)
def test_a_moderator_key_checks_the_zuliprc_where_it_can_read_the_address(
    laptop, capsys, visibility, said
):
    """#397's decision: the provisioner, `zab-tester` on the rig, is a Moderator. A
    Moderator's key reads an address visible to moderators, and then checks the zuliprc's
    e-mail, refusing another one; it can't read one visible to admins only. Its dry run fails
    the bots it would move, since it isn't an administrator's (#758), but checks the zuliprc
    all the same: a wrong one refuses it (exit 2)."""
    realm = member_realm(laptop)
    realm.users[41]["role"] = MODERATOR
    realm.users[8]["visibility"] = visibility
    zuliprc = ("--provisioner-zuliprc", str(laptop / "provisioner.zuliprc"))
    assert move_bots.main(member_move(laptop, "--provisioner-id", "8", *zuliprc),
                          transport=realm.transport()) == 1  # fmt: skip
    out = capsys.readouterr().out
    shown = "prov@example.test" if visibility == MODERATORS else "address hidden from this key"
    assert f"to zab-provisioner <{shown}> (8, member)\n{said}" in out
    (laptop / "provisioner.zuliprc").write_text(
        "[api]\nemail=tester@example.test\nkey=" + "m" * 32 + "\nsite=https://org.zulipchat.com\n"
    )
    code = move_bots.main(member_move(laptop, "--provisioner-id", "8", *zuliprc),
                          transport=realm.transport())  # fmt: skip
    assert code == (2 if visibility == MODERATORS else 1)


@pytest.mark.parametrize(
    "role, visibility, readable",
    [
        (MEMBER, MODERATORS, False),  # the rig on Oct 15
        (MEMBER, MEMBERS, True),  # the org's default for new users since Oct 15 (#397)
        (MEMBER, EVERYONE, True),
        (MODERATOR, MODERATORS, True),
        (MODERATOR, ADMINS, False),
        (OWNER, ADMINS, True),
        (OWNER, NOBODY, False),
    ],
)
def test_the_fake_realm_shows_addresses_as_zulip_does(role, visibility, readable):
    """`can_access_delivery_email` on Zulip `main`; `email` is the dummy for every caller unless
    the visibility is everyone (`do_change_user_setting`)."""
    realm = MemberRealm()
    realm.human(50, "Caller", "caller@example.test", role=role)
    realm.human(60, "Target", "target@example.test", visibility=visibility)
    seen = realm.seen(realm.users[50], realm.users[60])
    assert seen["delivery_email"] == ("target@example.test" if readable else None)
    assert seen["email"] == ("target@example.test" if visibility == EVERYONE else
                             f"user60@{realm.host}")  # fmt: skip
    assert (realm.lookup(realm.users[50], "target@example.test") is not None) == readable
    assert realm.seen(realm.users[60], realm.users[60])["delivery_email"] == "target@example.test"


def test_join_reads_the_identitys_public_half_with_ssh_keygen(tmp_path):
    """The real `ssh-keygen -y`, on an unencrypted key whose `.pub` is gone."""
    from admin_common import subprocess_runner

    key = tmp_path / "id"
    made = subprocess_runner(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    if not made.ok:
        pytest.skip(f"no ssh-keygen here: {made.stderr}")
    public = (tmp_path / "id.pub").read_text().split()[:2]
    (tmp_path / "id.pub").unlink()
    assert join_server.own_key(str(key), subprocess_runner) == public
