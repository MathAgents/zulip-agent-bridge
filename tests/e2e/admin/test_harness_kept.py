"""A18's laptop side (`harness/kept.py`), offline (docs/ADMIN.md §7, §8.4; #356, #358): the
scripts' argument lists, their tables, the stage of the cutover's O4 (ADMIN §8.4 at v0.2.15)
through the real `stage_server.py`, the waits of A18 through `settle`, and the bots and the
sandbox made ready before a rerun, never by an owner change (#758)."""

from __future__ import annotations

import base64
import functools
import importlib
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2e.admin.harness import kept, settle
from e2e.admin.harness import rig as rigs
from e2e.admin.harness.kept import KINDS, SLUG, TRANSITION_S, Laptop, row_of, table
from e2e.harness.config import CHECKOUT
from e2e.harness.secret_files import SecretFileError
from e2e.harness.vm import Result
from zab.zulip.client import ZulipError

IP = "198.51.100.9"
HOST_KEY = f"{IP} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx0ZXN0a2V5Zm9ydGhlYTE4c3RhZ2VvZmZsaW5l\n"


@pytest.fixture
def scripts(monkeypatch):
    """The admin scripts, imported by path as tests/workshop does."""
    for sub in ("aws", "admin"):
        monkeypatch.syspath_prepend(str(CHECKOUT / "workshop" / sub))
    return SimpleNamespace(**{name: importlib.import_module(name) for name in (
        "admin_common", "aws_common", "zulip_provision", "scan_hosts", "join_server")})  # fmt: skip


def laptop_of(tmp_path, run=kept.local) -> Laptop:
    rig = rigs.load({"HOME": str(tmp_path)})
    (tmp_path / "laptop").mkdir(mode=0o700)
    return Laptop(tmp_path / "laptop", rig, run)


def launched(laptop: Laptop) -> None:
    """The laptop's roster as launch.py leaves it: the kept row with its VM's address, and the
    torn-down row as it was (its old address, out of keep.txt)."""
    laptop.write_roster("t2@example.com", "us-east-1")
    rows = kept.read_csv(laptop.roster)
    kept.write_csv(laptop.roster, [r | {"ip": IP} if r["slug"] == SLUG else r for r in rows])


def test_the_laptops_roster_is_oct_9s_with_a_row_torn_down_since(tmp_path):
    laptop = laptop_of(tmp_path)
    launched(laptop)
    assert [(r["slug"], r["ip"]) for r in kept.read_csv(laptop.roster)] == [
        (SLUG, IP), (kept.GONE, kept.GONE_IP)]  # fmt: skip
    kept_slugs = [ln for ln in laptop.keep.read_text().splitlines() if not ln.startswith("#")]
    assert kept_slugs == [SLUG] and laptop.keep.read_text().startswith("#")  # with a comment
    assert laptop.row()["slug"] == SLUG


def test_a_bots_email_is_read_and_a_malformed_zuliprc_named_never_quoted(tmp_path):
    """#519: configparser quotes the offending line; the error names the file, in the
    laptop's directory, and the exception's type. A decoy key."""
    laptop = laptop_of(tmp_path)
    rc = laptop.out / SLUG / "codex.zuliprc"
    rc.parent.mkdir(parents=True)
    rc.write_text("[api]\nemail = c@zulip.example\nkey=k\n")
    assert laptop.bot_email("codex") == "c@zulip.example"
    rc.write_text("[api]\nemail=c@zulip.example\nDECOY-SECRET-519\n")
    with pytest.raises(SecretFileError) as caught:
        laptop.bot_email("codex")
    assert str(caught.value) == f"out/{SLUG}/codex.zuliprc: ParsingError"
    with pytest.raises(SecretFileError, match=r"claude\.zuliprc: NoSectionError$"):
        laptop.bot_email("claude")  # not written


def test_each_script_gets_the_rigs_inputs_and_only_those(tmp_path, monkeypatch):
    calls = []
    laptop = laptop_of(tmp_path, lambda argv, env, timeout: calls.append((argv, env)) or Result(
        0, "", ""))  # fmt: skip
    monkeypatch.setenv("ZAB_SSH_IDENTITY", "/elsewhere/key")
    sources = ["198.51.100.2/32", "203.0.113.7/32"]
    laptop.launch(ami="ami-0123456789abcdef0", group="sg-0123456789abcdef1", ssh_from=sources)
    laptop.provision(laptop.rig.tester2_zuliprc, "--hold")
    laptop.scan()
    laptop.stage(laptop.dir / "stage")
    laptop.join(dry_run=True, remove=True)
    laptop.move(laptop.rig.tester2_zuliprc, laptop.rig.provisioner_zuliprc, "zab-tester", 11,
                apply=True)  # fmt: skip
    for argv, env in calls:
        script = argv[1]
        assert script.startswith(str(CHECKOUT / "workshop")) and "ZAB_SSH_IDENTITY" not in env
        usage = subprocess.run([sys.executable, script, "--help"], capture_output=True,  # noqa: S603
                               text=True, check=True).stdout  # fmt: skip
        for flag in (a for a in argv[2:] if a.startswith("--")):  # each one the script takes
            assert re.search(rf"(?<![\w-]){flag}(?![\w-])", usage), f"{script} takes no {flag}"
        if script.endswith(("stage_server.py", "join_server.py", "move_bots.py")):  # as §8.4
            assert argv[argv.index("--keep") + 1] == str(laptop.keep) and "--only" not in argv
        else:  # as §7 gives the setup, and part 9
            assert argv[argv.index("--only") + 1] == SLUG
        assert all(not a.startswith(str(CHECKOUT / "workshop" / "admin" / "out")) for a in argv)
    launch = calls[0][0]
    scope = [launch[i + 1] for i, a in enumerate(launch) if a == "--tag"]
    # its workshop, the rig's scope (#422: the server finds it by all three), test infrastructure's
    assert scope == ["workshop=zab-admin-e2e2", "zab-scope=admin-e2e", "zab-role=test"]
    assert launch[launch.index("--key-name") + 1] == "zab-test"  # the break-glass key's pair
    assert "--admin-key" not in launch  # launched without the server's key, as on Oct 9
    # launch.py's own check: the group admits SSH from these and from nothing else
    assert [launch[i + 1] for i, a in enumerate(launch) if a == "--ssh-from"] == sources
    with pytest.raises(ValueError, match="expected SSH sources"):
        laptop.launch(ami="ami-0123456789abcdef0", group="sg-0123456789abcdef1", ssh_from=[])
    assert launch[launch.index("--spares-file") + 1].startswith(str(laptop.dir))
    join = calls[4][0]
    assert join[join.index("--identity") + 1] == str(laptop.rig.ssh_key)
    assert join[join.index("--server-key") + 1] == str(laptop.dir / "zab-admin-fleet.pub")
    assert {"--remove", "--dry-run"} <= set(join)
    move = calls[5][0]
    assert move[move.index("--to") + 1] == "provisioner" and "--apply" in move
    assert move[move.index("--provisioner-id") + 1] == "11"


def test_the_laptop_lives_in_the_rigs_directory_and_goes_whatever_ends_a18(tmp_path):
    """It holds the bots' zuliprcs: never in /tmp, and never left behind (review of #381)."""
    rig = rigs.load({"HOME": str(tmp_path)})
    rig.dir.mkdir(parents=True, mode=0o700)
    stale = rig.dir / f"{kept.LAPTOP_PREFIX}killed"  # a run killed before its finally
    stale.mkdir()
    (rig.dir / "keys").mkdir()  # the rig's own: left alone
    with pytest.raises(RuntimeError, match="a part failed"), kept.laptop_dir(rig) as path:
        assert path.parent == rig.dir and path.stat().st_mode & 0o777 == 0o700
        assert not stale.exists()
        (path / "out").mkdir()
        raise RuntimeError("a part failed")
    assert sorted(p.name for p in rig.dir.iterdir()) == ["keys"]


def test_the_scripts_tables_are_read_by_their_columns(scripts):
    """Each table as its script renders it; a cell may hold spaces."""
    report = scripts.zulip_provision.Report(
        SLUG, bots={"codex": "reused", "claude": "reused"}, channel="exists",
        notes=["a note, two words"],
    )  # fmt: skip
    other = scripts.zulip_provision.Report("zab-test-x", bots={"codex": "would create"})
    done = Result(1, scripts.zulip_provision.render([other, report]), "1 participant(s) need")
    row = row_of(done, "participant")
    assert (row["bots"], row["channel"], row["notes"]) == (
        "codex: reused, claude: reused", "exists", "a note, two words")  # fmt: skip
    scan = scripts.scan_hosts.Scan(SLUG, IP, "added", "SHA256:abc", "verified")
    row = row_of(Result(0, scripts.scan_hosts.render([scan]) + "\n\n1 key(s) written", ""),
                 "participant")  # fmt: skip
    assert (row["key"], row["console"]) == ("added", "verified")
    moves = [[SLUG, kind, "would move from user 12"] for kind in KINDS]
    text = "DRY RUN: as …\n" + scripts.admin_common.format_table(["slug", "bot", "result"], moves)
    assert {r["bot"]: r["result"] for r in table(text + "\n0 participant bots", "slug")} == (
        dict.fromkeys(KINDS, "would move from user 12"))  # fmt: skip
    joined = scripts.admin_common.format_table(
        ["slug", "ip", "result"], [[SLUG, IP, "ok: added"], [kept.GONE, "", "skip: no VM"]]
    )
    rows = table(joined + "\n1 of 2 ok", "slug")  # every row; not the summary
    assert [r["slug"] for r in rows] == [SLUG, kept.GONE]
    with pytest.raises(AssertionError, match="2 rows of zab-test-c"):
        row_of(Result(0, text, ""), "slug")
    with pytest.raises(AssertionError, match="no table"):
        row_of(Result(2, "", "join refused: --identity: expected a private key file"), "slug")


def test_join_servers_own_table(tmp_path, scripts, capsys):
    """join_server.py's run with `--keep`, as the laptop runs it, and a fake SSH: what A18's
    `joined` reads from it. Only the kept VM is reached, never the torn-down row's address."""
    laptop = laptop_of(tmp_path)
    launched(laptop)
    identity, server = laptop.dir / "id_ed25519", laptop.server_key
    identity.write_text("a private key\n")
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    server.write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " zab-admin@srv\n")
    argv = ["--roster", str(laptop.roster), "--keep", str(laptop.keep), "--identity",
            str(identity), "--server-key", str(server), "--known-hosts",
            str(laptop.known_hosts)]  # fmt: skip
    for said, dry, result in (("absent", True, "would add"), ("added", False, "ok: added")):
        reached = []

        def fake(argv, *args, said=said, reached=reached, **kwargs):  # the remote's answer
            reached.append(next(a for a in argv if a.startswith("ubuntu@")))
            return scripts.admin_common.RunResult(0, said + "\n", "")

        code = scripts.join_server.main([*argv, *(["--dry-run"] * dry)], runner=fake)
        out = capsys.readouterr().out
        assert code == 0 and row_of(Result(0, out, ""), "slug")["result"] == result
        assert [r["slug"] for r in table(out, "slug")] == [SLUG] and reached == [f"ubuntu@{IP}"]


def on_this_box(literal: str, state, config) -> str:
    """A server script as it runs here: `/var/lib/zab-admin` is `state`, admin.toml `config`,
    the release this checkout with this Python; no sudo and no chown (one user here). Every
    server path must be swapped, so a script whose shape changed fails here, never runs half."""
    swaps = (
        ("sudo -u zab-admin ", ""),
        ('chown zab-admin:zab-admin "$t"', ":"),
        ("/opt/zab-admin/current/.venv/bin/python", sys.executable),
        ("/opt/zab-admin/current/workshop", str(CHECKOUT / "workshop")),
        ("/etc/zab-admin/admin.toml", str(config)),
        ("/var/lib/zab-admin", str(state)),
    )
    for old, new in swaps:
        literal = literal.replace(old, new)
    assert not re.search(r"sudo|chown|/opt/|/etc/zab|/var/lib", literal), literal
    return literal


def run_here(literal: str, state, config, *args: str, stdin: str = "") -> Result:
    done = subprocess.run(["bash", "-c", on_this_box(literal, state, config), "bash", *args],  # noqa: S603, S607
                          capture_output=True, text=True, check=False, input=stdin)  # fmt: skip
    return Result(done.returncode, done.stdout, done.stderr)


def server_state(tmp_path):
    """The server's state as A18 finds it: zab-admin-e2e's roster (A2's), e2e2's directory
    (the setup's reset, A15's roster), the retired slugs, and admin.toml's two workshops."""
    state = tmp_path / "server"
    for ws, slug in (("zab-admin-e2e", "zab-test-a"), ("zab-admin-e2e2", SLUG)):
        (state / "ws" / ws / "out").mkdir(parents=True)
        (state / "ws" / ws / "roster.csv").write_text(f"slug,email\n{slug},{slug}@example.com\n")
    (state / "retired-slugs.txt").write_text("")
    config = state / "admin.toml"
    config.write_text("".join(
        f'[[workshop]]\nname = "{ws}"\nroster = "{state}/ws/{ws}/roster.csv"\n'
        for ws in ("zab-admin-e2e", "zab-admin-e2e2")))  # fmt: skip
    return state, config


def test_fresh_removes_the_second_workshops_state_and_nothing_else(tmp_path):
    state, config = server_state(tmp_path)
    for _ in range(2):  # a rerun finds nothing to remove
        assert run_here(kept.FRESH, state, config, "zab-admin-e2e2").ok
        assert sorted(p.name for p in (state / "ws").iterdir()) == ["zab-admin-e2e"]
    theirs = (state / "ws" / "zab-admin-e2e" / "roster.csv").read_text()
    assert theirs == "slug,email\nzab-test-a,zab-test-a@example.com\n"  # untouched


def test_o4_stages_the_row_and_the_server_refuses_another_workshops_slug(tmp_path):
    """The stage made by the real stage_server.py on the laptop (`--keep`: the torn-down row
    stays out), packed as `STAGE` reads it, then `STAGE` itself, run here (`on_this_box`): the
    row lands in e2e2's directory; a stage that also holds zab-admin-e2e's slug is refused,
    and nothing lands."""
    laptop = laptop_of(tmp_path)
    launched(laptop)
    (laptop.out / SLUG).mkdir(parents=True)
    for kind in KINDS:  # zulip_provision.py's
        (laptop.out / SLUG / f"{kind}.zuliprc").write_text(f"[api]\nemail={kind}@z\nkey=SECRET\n")
    laptop.known_hosts.write_text(HOST_KEY)  # scan_hosts.py's
    stage, refused = laptop.dir / "stage", laptop.dir / "stage-refused"
    done = laptop.stage(stage)  # zab-test-d has no keys in out/: selected, it would refuse
    assert done.ok and "staged 1 rows, 2 bot keys" in done.stdout, done.stderr
    kept.refused_stage(stage, refused, "zab-test-a")
    packed = kept.stage_tar(stage)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(packed))) as tar:
        names = sorted(m.name for m in tar.getmembers())
        assert all(m.uid == 0 and m.uname == "" for m in tar.getmembers())
    assert names == ["out", "out/known_hosts", f"out/{SLUG}", f"out/{SLUG}/claude.zuliprc",
                     f"out/{SLUG}/codex.zuliprc", "roster.csv"]  # fmt: skip
    assert b"SECRET" not in base64.b64decode(kept.stage_tar(refused))  # no key travels for it
    state, config = server_state(tmp_path)
    assert run_here(kept.FRESH, state, config, "zab-admin-e2e2").ok  # as parts 2 and 3 do
    ws = state / "ws"
    done = run_here(kept.STAGE, state, config, "zab-admin-e2e2", stdin=kept.stage_tar(refused))
    assert done.returncode == 2 and "zab-test-a is in zab-admin-e2e" in done.stderr, done.stderr
    assert sorted(p.name for p in ws.iterdir()) == ["zab-admin-e2e"]  # nothing landed
    done = run_here(kept.STAGE, state, config, "zab-admin-e2e2", stdin=packed)
    assert done.ok and "staged 1 rows, 2 bot keys and known_hosts" in done.stdout, done.stderr
    assert sorted(p.name for p in ws.iterdir()) == ["zab-admin-e2e", "zab-admin-e2e2"]
    landed = ws / "zab-admin-e2e2"
    assert [r["slug"] for r in kept.read_csv(landed / "roster.csv")] == [SLUG]
    assert (landed / "out" / SLUG / "codex.zuliprc").read_text().endswith("key=SECRET\n")
    assert sorted(p.name for p in (ws / "zab-admin-e2e").iterdir()) == ["out", "roster.csv"]


def test_a_stage_with_a_link_is_never_packed(tmp_path):
    stage = tmp_path / "stage"
    (stage / "out").mkdir(parents=True)
    (stage / "roster.csv").write_text("slug\n")
    (stage / "out" / "known_hosts").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="neither a file nor a directory"):
        kept.stage_tar(stage)


@pytest.mark.parametrize("literal", [kept.FRESH, kept.STAGE, kept.STANDIN])
def test_the_server_scripts_refuse_any_other_workshop(literal):
    """Before any command: the first workshop's state, or another VM's, is never touched."""
    for ws in ("zab-admin-e2e", "../zab-admin-e2e2", "", "../zab-test-c", "zab-test-a"):
        done = subprocess.run(["bash", "-c", literal, "bash", ws], capture_output=True,  # noqa: S603, S607
                              text=True, check=False, input="")  # fmt: skip
        assert done.returncode == 2 and done.stderr.startswith("refused:"), (ws, done.stderr)
    assert "--config /etc/zab-admin/admin.toml --workshop" in kept.STAGE  # a slug's (§2.1)
    assert "--retired /var/lib/zab-admin/retired-slugs.txt" in kept.STAGE


def test_the_stand_in_checkout_goes_over_the_break_glass_key(tmp_path, scripts):
    """As the laptop reaches a kept VM: `ubuntu`, the rig's key alone, the pinned host key;
    the checkout is the one push_config.py names (`default_project`: ~/work/<work_repo>)."""
    calls = []
    laptop = laptop_of(tmp_path, lambda argv, env, timeout: calls.append(argv) or Result(
        0, "initialized\n", ""))  # fmt: skip
    launched(laptop)
    assert laptop.stand_in_checkout().stdout == "initialized\n"
    (argv,) = calls
    assert argv[0] == "ssh" and argv[argv.index("-i") + 1] == str(laptop.rig.ssh_key)
    assert {"IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no"} <= set(argv)
    assert f'UserKnownHostsFile="{laptop.known_hosts}"' in argv
    assert argv[-3:-1] == [f"ubuntu@{IP}", "--"]
    assert shlex.split(argv[-1]) == ["bash", "-c", kept.STANDIN, "bash", SLUG]
    assert scripts.aws_common.work_repo(kept.read_csv(laptop.roster)[0]) == SLUG


def test_the_stand_in_checkout_retries_a_connect_failure_into_the_notes(tmp_path, monkeypatch):
    """#517 item 4 (reviewer on #535): A18's connect failure is tried again and noted where
    the scenario passes its logger (`conftest.note_ssh`), not only on dropped stderr."""
    answers = [Result(255, "", f"ssh: connect to host {IP} port 22: Connection timed out\n"),
               Result(0, "exists\n", "")]  # fmt: skip
    laptop = laptop_of(tmp_path, lambda argv, env, timeout: answers.pop(0))
    launched(laptop)
    slept, notes = [], []
    monkeypatch.setattr(kept, "Vm", functools.partial(kept.Vm, sleep=slept.append))
    assert laptop.stand_in_checkout(log=notes.append).stdout == "exists\n"
    assert slept == [5.0] and not answers
    (note,) = notes
    assert note.startswith("SSH connect failure (bash -c ") and note.endswith("; again in 5 s")


def test_the_stand_in_is_made_once_and_passes_the_doctors_check(tmp_path):
    """`STANDIN` itself, run here with `sudo -u agent` dropped and `agent`'s home moved into a
    temporary directory: an empty repository at work/<slug>, then `exists`, changing nothing;
    and the doctor's check of a project (`git rev-parse --show-toplevel`) passes on it."""
    home = tmp_path / "agent"
    home.mkdir()
    here = kept.STANDIN.replace("sudo -u agent ", "").replace("/home/agent", str(home))
    assert "sudo" not in here and "/home/agent" not in here
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def run(*argv: str) -> str:
        done = subprocess.run(argv, capture_output=True, text=True, check=True, env=env)  # noqa: S603
        return done.stdout

    said = [run("bash", "-c", here, "bash", SLUG).strip() for _ in range(2)]
    assert said == ["initialized", "exists"]
    checkout = home / "work" / SLUG
    assert Path(run("git", "-C", str(checkout), "rev-parse", "--show-toplevel").strip()) == (
        checkout.resolve())  # fmt: skip
    assert sorted(p.name for p in checkout.iterdir()) == [".git"]  # nothing but the repository


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def test_a18_waits_for_a_transition_through_settle_within_its_bound(monkeypatch):
    """A stop, a start or a wake: read again until EC2 shows it, at most TRANSITION_S (#378)."""
    from e2e.admin import test_scenarios_8_the_kept_fleet as a18

    clock = Clock()
    monkeypatch.setattr(settle, "clock", lambda: clock.now)
    monkeypatch.setattr(settle, "sleep", clock.sleep)
    states = iter(["stopping", "stopping", "stopped"])
    monkeypatch.setattr(a18, "state", lambda aws: next(states))
    await a18.in_state(None, "stopped")
    assert clock.now == 20
    monkeypatch.setattr(a18, "state", lambda aws: "stopped")
    clock.now = 0.0
    with pytest.raises(AssertionError, match="zab-test-c running: still 'stopped' after 300 s"):
        await a18.in_state(None, "running")
    assert TRANSITION_S <= clock.now <= TRANSITION_S + 10  # the bound, not longer


def test_the_bots_emails_and_names_are_zulip_provisions(scripts):
    for kind, email in kept.bot_emails("zulip.example").items():
        assert email == f"{scripts.zulip_provision.bot_short_name(kind, SLUG)}-bot@zulip.example"
        assert kept.bot_name(kind) == scripts.zulip_provision.bot_full_name(kind, SLUG)
    assert kept.sandbox_name() == scripts.zulip_provision.channel_name(SLUG)


class Realm:
    """The bots and the channels as two test users see them, a change only by a user who may
    make it: a bot's reactivation by its owner, a channel's rename by its administrator, and
    never a bot's owner change, which Zulip Cloud refuses to every test user (#758). With
    `applies=False`, Zulip acknowledges a change and doesn't make it; `lookup` is the error a
    lookup by e-mail answers with (Zulip's "no such user" by default). `channels`: ID → (name,
    the IDs of the users who see and administer it); `rename`, the error a rename answers with,
    if any."""

    TESTER, TESTER2 = 11, 12

    def __init__(
        self,
        bots: dict[str, dict],
        *,
        applies: bool = True,
        lookup=None,
        channels=None,
        rename=None,
    ) -> None:
        self.users = {u["user_id"]: u for u in bots.values()}
        self.by_email = {e: u["user_id"] for e, u in bots.items()}
        self.channels: dict[int, tuple[str, set[int]]] = dict(channels or {})
        self.rename = rename
        self.changes: list[tuple[int, str, str]] = []
        self.applies, self.lookup = applies, lookup or ZulipError("BAD_REQUEST", 400)

    def client(self, me: int):
        realm = self

        class Client:
            async def request(self, method: str, endpoint: str, **p):
                parts = endpoint.split("/")
                if method == "GET" and endpoint == "streams":
                    assert p == {"include_all_active": True}
                    seen = [(sid, name) for sid, (name, admins) in realm.channels.items()
                            if me in admins]  # fmt: skip
                    return {"streams": [{"stream_id": s, "name": n} for s, n in seen]}
                if method == "GET" and "@" in endpoint:
                    if parts[1] not in realm.by_email:
                        raise realm.lookup
                    return {"user": dict(realm.users[realm.by_email[parts[1]]])}
                if method == "PATCH" and parts[0] == "streams":
                    name, admins = realm.channels[int(parts[1])]
                    assert me in admins, f"{me} may not rename {name}"
                    realm.changes.append((me, method, endpoint))
                    if realm.rename is not None:
                        raise realm.rename
                    realm.channels[int(parts[1])] = (p["new_name"], admins)
                    return {}
                user = realm.users[int(parts[1])]
                if method == "GET":
                    return {"user": dict(user)}
                realm.changes.append((me, method, endpoint))
                if method == "PATCH":  # the bot's owner change: never an administrator here
                    raise ZulipError("UNAUTHORIZED_PRINCIPAL", 400)
                assert user["bot_owner_id"] == me, f"{me} may not change {user['user_id']}"
                if realm.applies:
                    user["is_active"] = True
                return {}

        return Client()


def bot(uid: int, owner: int, active: bool, kind: str = "codex", **more) -> dict:
    return {"user_id": uid, "is_bot": True, "bot_owner_id": owner, "is_active": active,
            "full_name": kept.bot_name(kind), **more}  # fmt: skip


IDS = SimpleNamespace(tester=Realm.TESTER, tester2=Realm.TESTER2)


async def test_a_previous_runs_bots_are_reactivated_by_zab_tester_who_keeps_them():
    """The rig on Nov 2 (#758): the Codex bot active, the Claude bot deactivated, both
    zab-tester's. Its owner reactivates one, and no owner ever changes."""
    emails = kept.bot_emails("z")
    realm = Realm({emails["codex"]: bot(1, Realm.TESTER, True),
                   emails["claude"]: bot(2, Realm.TESTER, False, "claude")})  # fmt: skip
    done = await kept.restore_bots(realm.client(11), IDS, emails)
    assert done == ["codex: zab-tester's, active already", "claude: zab-tester's, reactivated"]
    assert realm.changes == [(11, "POST", "users/2/reactivate")]
    assert all(u["is_active"] and u["bot_owner_id"] == 11 for u in realm.users.values())
    realm.changes.clear()
    again = await kept.restore_bots(realm.client(11), IDS, emails)
    assert again == [f"{kind}: zab-tester's, active already" for kind in KINDS]
    assert realm.changes == []
    first = await kept.restore_bots(realm.client(11), IDS, kept.bot_emails("y"))
    assert first == ["codex: none yet", "claude: none yet"]  # zulip_provision.py makes them


async def test_zab_tester2s_bots_are_left_alone_before_anything_changes():
    """Bots A18 made with zab-tester2's key before #758: only an organization administrator can
    give them to zab-tester, and a new bot can't take their names, so the setup stops, saying
    so, and reactivates nothing, not even the other bot."""
    emails = kept.bot_emails("z")
    realm = Realm({emails["codex"]: bot(1, Realm.TESTER, False),
                   emails["claude"]: bot(2, Realm.TESTER2, False, "claude")})  # fmt: skip
    with pytest.raises(LookupError, match="only an organization administrator can give it to"):
        await kept.restore_bots(realm.client(11), IDS, emails)
    assert realm.changes == []


@pytest.mark.parametrize(
    "user, why",
    [
        (bot(1, 99, False), "not a bot of zab-tester's"),  # someone else's
        (bot(1, Realm.TESTER, False, is_bot=False), "not a bot of zab-tester's"),  # a person
        (bot(1, Realm.TESTER2, False, is_bot=False), "not a bot of zab-tester's"),
        (bot(1, Realm.TESTER, False, full_name="Codex · zab-test-x"), "is named"),
    ],
)
async def test_anything_but_the_rows_own_bot_is_left_alone(user, why):
    """Matched as move_bots.py and `--deactivate` match a bot: a bot, its name, its owner."""
    emails = kept.bot_emails("z")
    realm = Realm({emails["codex"]: user})
    with pytest.raises(LookupError, match=why):
        await kept.restore_bots(realm.client(11), IDS, emails)
    assert realm.changes == []


async def test_a_change_zulip_did_not_make_is_found_by_the_read_back():
    emails = kept.bot_emails("z")
    realm = Realm({emails["codex"]: bot(1, Realm.TESTER, False)}, applies=False)
    with pytest.raises(LookupError, match="active False, owner 11"):
        await kept.restore_bots(realm.client(11), IDS, emails)


@pytest.mark.parametrize("error", [ZulipError("UNAUTHORIZED", 401), ZulipError("RATE", 429)])
async def test_only_no_such_user_reads_as_not_made_yet(error):
    """A refused or rate-limited lookup is raised, never taken for a bot not made yet."""
    realm = Realm({}, lookup=error)
    with pytest.raises(ZulipError) as raised:
        await kept.restore_bots(realm.client(11), IDS, kept.bot_emails("z"))
    assert raised.value is error and realm.changes == []


@pytest.mark.parametrize(
    "channels, said, after",
    [
        ({5: ("zab-test-c-sandbox", {11})}, "zab-test-c-sandbox: seen by zab-tester", None),
        ({5: ("Zab-Test-C-Sandbox", {11, 12})}, "zab-test-c-sandbox: seen by zab-tester", None),
        ({}, "zab-test-c-sandbox: none yet", None),
        ({5: ("zab-test-a-sandbox", {12})}, "zab-test-c-sandbox: none yet", None),
        (
            {5: ("zab-test-c-sandbox", {12}), 6: ("other", {11})},
            "zab-test-c-sandbox: zab-tester2's, renamed zab-test-c-sandbox-old-5 by zab-tester2",
            "zab-test-c-sandbox-old-5",
        ),
    ],
)
async def test_a_sandbox_zab_tester_cannot_see_is_renamed_out_of_its_way(channels, said, after):
    """Before #758 A18 made the sandbox with zab-tester2's key, its administrator then, and
    zab-tester can't see it, while Zulip refuses a second channel of that name: zab-tester2
    renames it once. One zab-tester sees, or none, is left alone."""
    realm = Realm({}, channels=channels)
    note = await kept.restore_sandbox(realm.client(11), realm.client(12))
    assert note.startswith(said)
    if after is None:
        assert realm.changes == [] and realm.channels == channels
    else:
        assert realm.changes == [(12, "PATCH", "streams/5")]
        assert realm.channels[5][0] == after


async def test_a_refused_rename_says_what_to_do_by_hand():
    error = ZulipError("BAD_REQUEST", 400)
    realm = Realm({}, channels={5: ("zab-test-c-sandbox", {12})}, rename=error)
    with pytest.raises(LookupError, match="Rename it so in Zulip, signed in as zab-tester2") as e:
        await kept.restore_sandbox(realm.client(11), realm.client(12))
    assert e.value.__cause__ is error and realm.channels[5][0] == "zab-test-c-sandbox"
