"""The root helper `zab-admin-config` (docs/ADMIN.md §2.9; #418, #332): it adds exactly one new
workshop's entry to admin.toml and writes its bot's key, after every check, and a refused
request changes no file."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from zab.admin import confighelper
from zab.admin.config import parse_admin_config
from zab.admin.confighelper import Paths, Refused

from .botkit import GLOBAL, HARVARD, OLGA, make_site

KEY = "A" * 32
EMAIL = "mit-2027-admin-bot@example.zulipchat.com"
ZULIPRC = f"[api]\nemail={EMAIL}\nkey={KEY}\nsite=https://example.zulipchat.com\n"
#: Deeper than the parser of Python 3.11 to 3.13 goes: a RecursionError there, refused as not
#: JSON (review of #421). Python 3.14 bounds the parser by the C stack itself and parses this,
#: a list, which the shape check refuses: refused either way, and no file changes (#622).
DEEP = b"[" * 20000 + b"]" * 20000


def too_deep_to_parse(data: bytes) -> bool:
    """Whether this Python's `json.loads` raises RecursionError on `data`."""
    try:
        json.loads(data)
    except RecursionError:
        return True
    return False


class Helper:
    """A server tree (`botkit`) with the helper's request directory and state directory."""

    def __init__(self, tmp: Path) -> None:
        self.site = make_site(tmp)
        self.tmp = tmp
        requests, state = tmp / "requests", tmp / "helper-state"
        requests.mkdir(mode=0o700)
        state.mkdir(mode=0o755)
        self.paths = Paths(
            requests=str(requests),
            config=str(self.site.config_path),
            state=str(state),
            roots=self.site.roots,
        )
        self.owner, self.group = os.getuid(), os.getgid()

    @property
    def etc(self) -> Path:
        return self.tmp / "etc"

    def entry(self, name: str = "mit-2027") -> dict:
        etc, ws = self.etc, self.tmp / "ws"
        return {
            "name": name,
            "github_org": "MathAgents",
            "refserve_url": "https://files.mathagents.org",
            "roster": f"{ws}/{name}/roster.csv",
            "out_dir": f"{ws}/{name}/out",
            "organizers": [OLGA],
            "alerts_channel": 77,
            "bot": {
                "zuliprc": f"{etc}/secrets/bots/{name}.zuliprc",
                "user_id": 903,
                "email": EMAIL,
                "channels": [44],
            },
            "aws": {
                "region": "us-east-1",  # of botkit's [global] regions
                "ami": "ami-0123456789abcdef1",
                "security_group": "sg-0123456789abcdef1",
                "instance_types": ["m7a.2xlarge"],
                "key_name": f"zab-admin-{name}",
                "admin_keys": [f"{etc}/break-glass/drew.pub"],
            },
        }

    def request(self, entry: dict | None = None, **changes: object) -> Path:
        body = {"version": 1, "plan": "P-42", "entry": entry or self.entry(), "zuliprc": ZULIPRC}
        path = Path(self.paths.requests) / "request.json"
        path.write_text(json.dumps(body | changes))
        return path

    def run(self) -> int:
        return confighelper.run(self.paths, self.owner, self.group)

    def result(self) -> dict:
        return json.loads((Path(self.paths.state) / "result.json").read_text())

    def files(self) -> dict[str, tuple[int, bytes]]:
        """Every file under etc/, with its mode and bytes."""
        return {
            str(p.relative_to(self.etc)): (p.stat().st_mode, p.read_bytes())
            for p in sorted(self.etc.rglob("*"))
            if p.is_file()
        }


@pytest.fixture
def helper(tmp_path):
    return Helper(tmp_path)


def test_a_new_workshop_is_added_with_its_key(helper):
    old = helper.site.config_path.read_text()
    request = helper.request()
    assert helper.run() == 0
    assert not request.exists()  # deleted once read
    text = helper.site.config_path.read_text()
    assert text.startswith(old) and (helper.etc / "admin.toml.bak").read_text() == old
    config = parse_admin_config(text, roots=helper.site.roots)
    assert [ws.name for ws in config.workshops] == ["harvard-2026", "zab-test", "mit-2027"]
    new = config.workshops[-1]
    assert new.tags == (("workshop", "mit-2027"), ("zab-scope", "mathagents"))  # [global]'s
    assert new.organizers == {OLGA} and new.alerts_channel == 77
    assert new.bot.user_id == 903 and new.bot.channels == {44}
    key = helper.etc / "secrets" / "bots" / "mit-2027.zuliprc"
    assert key.read_text() == ZULIPRC
    assert key.stat().st_mode & 0o7777 == 0o640 and key.stat().st_gid == helper.group
    assert (helper.etc / "admin.toml").stat().st_mode & 0o777 == 0o644
    assert sorted(p.name for p in helper.etc.iterdir()) == [
        "admin.toml", "admin.toml.bak", "backup_recipients", "break-glass", "secrets",
    ]  # fmt: skip
    result = helper.result()
    assert result["ok"] is True and (result["plan"], result["workshop"]) == ("P-42", "mit-2027")
    assert result["admin_toml_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    (line,) = (Path(helper.paths.state) / "changes.jsonl").read_text().splitlines()
    record = json.loads(line)
    assert record["event"] == "new-workshop" and record["plan"] == "P-42"
    assert (record["workshop"], record["organizers"], record["bot"]) == ("mit-2027", [OLGA], 903)
    assert record["before_sha256"] == hashlib.sha256(old.encode()).hexdigest()
    assert record["after_sha256"] == result["admin_toml_sha256"]
    assert KEY not in line and KEY not in json.dumps(result)


def test_an_entry_with_its_participants_channel_is_added(helper):
    """#627: `channel`, the participants' channel's ID, after `alerts_channel`."""
    entry = helper.entry() | {"channel": 79}
    helper.request(entry)
    assert helper.run() == 0
    text = helper.site.config_path.read_text()
    assert "\nalerts_channel = 77\nchannel = 79\n[workshop.bot]\n" in text
    config = parse_admin_config(text, roots=helper.site.roots)
    assert config.workshops[-1].channel == 79


def test_an_entry_whose_channel_is_its_bots_is_refused(helper):
    helper.request(helper.entry() | {"channel": 44})  # its bot's command channel
    refused(helper, "a workshop's channel is another workshop's, a bot's command channel")


def test_under_the_units_umask_the_bot_can_read_what_it_must(helper):
    """The unit runs with UMask=0077 (Codex on #424): the record and the outcome stay 0644, for
    the bot (`zab-admin`) to read; the key stays 0640, admin.toml 0644."""
    helper.request()
    old = os.umask(0o077)
    try:
        assert helper.run() == 0
    finally:
        os.umask(old)
    state = Path(helper.paths.state)
    assert (state / "changes.jsonl").stat().st_mode & 0o777 == 0o644
    assert (state / "result.json").stat().st_mode & 0o777 == 0o644
    assert (helper.etc / "admin.toml").stat().st_mode & 0o777 == 0o644
    key = helper.etc / "secrets" / "bots" / "mit-2027.zuliprc"
    assert key.stat().st_mode & 0o777 == 0o640


def test_no_request_is_a_quiet_pass(helper):
    assert helper.run() == 0
    assert not (Path(helper.paths.state) / "result.json").exists()


def refused(helper: Helper, reason: str) -> None:
    before = helper.files()
    assert helper.run() == 1
    assert helper.files() == before  # #332: a refused request changes no file
    assert os.listdir(helper.paths.requests) == []  # the request, and no claimed copy, gone
    assert not (Path(helper.paths.state) / "changes.jsonl").exists()
    result = helper.result()
    assert result["ok"] is False and reason in result["message"], result["message"]
    assert KEY not in result["message"]


def test_a_request_naming_an_existing_workshop_changes_no_file(helper):
    """#332's criterion: a request naming an existing workshop changes no file."""
    entry = helper.entry("harvard-2026")
    helper.request(entry)
    refused(helper, "harvard-2026 is a workshop already")


def entry_with(path: str, value: object):
    """An edit of the entry: `aws.region` sets the subtable's key; None removes the key."""

    def edit(entry: dict) -> dict:
        entry = copy.deepcopy(entry)
        *tables, key = path.split(".")
        where = entry
        for table in tables:
            where = where[table]
        if value is None:
            where.pop(key)
        else:
            where[key] = value
        return entry

    return edit


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (entry_with("admins", [OLGA]), "a key admin.toml doesn't know"),
        (entry_with("global", {"user_id": 1}), "a key admin.toml doesn't know"),
        (entry_with("aws.role", "x"), "aws table is missing or has an unknown key"),
        (entry_with("bot", None), "the new bot's key must be"),
        (entry_with("bot.zuliprc", "{etc}/secrets/global.zuliprc"), "the new bot's key must be"),
        (entry_with("bot.zuliprc", "{etc}/secrets/bots/harvard-2026.zuliprc"), "must be"),
        (entry_with("bot.user_id", HARVARD), "two bots have the same user_id"),
        (entry_with("bot.user_id", True), "a boolean"),
        (entry_with("bot.channels", [42]), "a channel is bound to two bots"),
        (entry_with("bot.email", "other@example.zulipchat.com"), "not the zuliprc's"),
        (entry_with("organizers", [GLOBAL]), "is an admin bot and on an allowlist"),
        (entry_with("organizers", [[OLGA]]), "not a string, an integer or a list of them"),
        (entry_with("tag", ["workshop=mit-2027"]), "a key admin.toml doesn't know"),  # #425
        (entry_with("roster", "{ws}/harvard-2026/roster.csv"), "the new admin.toml is refused"),
        (entry_with("aws.instance_types", []), "the new admin.toml is refused"),
        (entry_with("refserve_url", 'x"\n[global]\nuser_id = 1'), "the new admin.toml is"),
        (entry_with("name", "Bad Name"), "not a workshop name"),
        (entry_with("name", "../../x"), "not a workshop name"),
        (entry_with("name", 7), "not a table with a name"),
    ],
)
def test_a_request_that_would_change_more_is_refused(helper, edit, reason):
    """Only a new `[[workshop]]` entry: never `admins`, `deployers`, `[global]` or another
    workshop, and nothing the loader refuses (§2.1, §2.9)."""
    entry = json.loads(
        json.dumps(edit(helper.entry()))
        .replace("{etc}", str(helper.etc))
        .replace("{ws}", str(helper.tmp / "ws"))
    )
    helper.request(entry)
    refused(helper, reason)


@pytest.mark.parametrize(
    ("zuliprc", "reason"),
    [
        (ZULIPRC.replace("[api]", "[other]"), "does not start with [api]"),
        (ZULIPRC + "insecure=true\n", "other lines than email, key and site"),
        (ZULIPRC + f"key={KEY}\n", "other lines than email, key and site"),
        (ZULIPRC.replace(KEY, KEY[:31]), "missing or malformed"),
        (ZULIPRC.replace("https://", "http://"), "missing or malformed"),
        ("[api]\nemail=a@b\nkey=" + KEY + "\n", "missing or malformed"),
    ],
)
def test_a_malformed_key_is_refused(helper, zuliprc, reason):
    helper.request(zuliprc=zuliprc)
    refused(helper, reason)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "not JSON"),
        (b"[]", "other keys"),
        (json.dumps({"version": 1, "plan": "P-1", "entry": {}}).encode(), "other keys"),
        (json.dumps({"version": 2, "plan": "P-1", "entry": {}, "zuliprc": ""}).encode(), "not "),
        (json.dumps({"version": 1, "plan": "P-0", "entry": {}, "zuliprc": ""}).encode(), "P-<n>"),
        (json.dumps({"version": 1, "plan": "P-1", "entry": [], "zuliprc": ""}).encode(), "table"),
        (b" " * (confighelper.MAX_REQUEST + 1), "over 65536 bytes"),
        (DEEP, "not JSON" if too_deep_to_parse(DEEP) else "other keys"),  # #421's review, #622
        (
            json.dumps({"version": True, "plan": "P-1", "entry": {}, "zuliprc": ""}).encode(),
            "not version 1",
        ),
        (
            json.dumps({"version": 1.0, "plan": "P-1", "entry": {}, "zuliprc": ""}).encode(),
            "not version 1",
        ),
    ],
)
def test_a_malformed_request_is_refused(helper, body, reason):
    (Path(helper.paths.requests) / "request.json").write_bytes(body)
    refused(helper, reason)


def test_a_request_too_deep_for_the_parser_is_not_json(helper, monkeypatch):
    """Review of #421: the parser's RecursionError is a refusal with an outcome, never an escape
    from `run`. Raised here on every Python, since 3.14's parser takes `DEEP` (#622)."""

    def loads(text):
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")

    monkeypatch.setattr(confighelper, "json", SimpleNamespace(loads=loads, dumps=json.dumps))
    helper.request()
    refused(helper, "the request is not JSON")


def test_an_existing_key_file_is_never_overwritten(helper):
    """#332: the key is written with O_EXCL. A file already there, say left by hand, refuses
    the request before any write."""
    stale = helper.etc / "secrets" / "bots" / "mit-2027.zuliprc"
    stale.write_text("stale\n")
    helper.request()
    refused(helper, "mit-2027.zuliprc exists")
    assert stale.read_text() == "stale\n"


def test_a_request_that_is_a_link_is_removed_and_refused(helper, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    link = Path(helper.paths.requests) / "request.json"
    link.symlink_to(target)
    refused(helper, "request.json")
    assert not link.is_symlink() and target.exists()


def test_a_request_that_is_a_directory_or_a_fifo_is_removed_and_refused(helper):
    """Review of #421: a directory named request.json is removed (an empty one), so the path
    unit stops firing; a FIFO likewise, with no blocking open."""
    request = Path(helper.paths.requests) / "request.json"
    request.mkdir()
    refused(helper, "not a regular file of zab-admin's")
    os.mkfifo(request)
    refused(helper, "not a regular file of zab-admin's")


def test_an_unexpected_error_still_writes_an_outcome(helper, monkeypatch):
    """Review of #421: whatever fails, the request has an outcome, and no file changed."""

    def broken(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(confighelper, "new_text", broken)
    helper.request()
    refused(helper, "internal error: RuntimeError")


def test_a_failure_after_the_renames_is_reported_as_applied(helper, monkeypatch):
    """Review of #421: once admin.toml is renamed into place the change is made; a later
    failure (the directories' fsync) is a warning on an applied outcome, not a refusal."""

    def no_sync(directory):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(confighelper, "_sync", no_sync)
    helper.request()
    assert helper.run() == 0
    result = helper.result()
    assert result["ok"] is True and "mit-2027 added to admin.toml" in result["message"]
    assert "but the directories' fsync failed: Input/output error" in result["message"]
    assert "mit-2027" in helper.site.config_path.read_text()


@pytest.mark.parametrize("fails", ["admin.toml.bak", "admin.toml"])
def test_a_failed_rename_removes_what_was_staged(helper, monkeypatch, fails):
    """Codex round 3 on #421: either rename failing leaves admin.toml as it was, and the key and
    the staged files are removed, so that a retry works; the outcome says the record has a
    line for it."""
    real = os.replace

    def replace(src, dst, *args, **kwargs):
        if os.path.basename(dst) == fails and str(src).endswith(".new"):
            raise OSError(5, "Input/output error")
        return real(src, dst, *args, **kwargs)

    old = helper.site.config_path.read_text()
    monkeypatch.setattr(confighelper.os, "replace", replace)
    helper.request()
    assert helper.run() == 1
    result = helper.result()
    assert result["ok"] is False and "changes.jsonl has a line for it" in result["message"]
    assert helper.site.config_path.read_text() == old
    assert not (helper.etc / "secrets" / "bots" / "mit-2027.zuliprc").exists()
    assert [n for n in os.listdir(helper.site.config_path.parent) if n.endswith(".new")] == []
    monkeypatch.undo()
    helper.request()
    assert helper.run() == 0 and "mit-2027" in helper.site.config_path.read_text()


def test_an_entry_naming_a_missing_break_glass_key_is_refused(helper):
    """Review of #421: every bot refuses to start without each break-glass key (checks), so
    such an entry is refused before anything is written."""
    entry = helper.entry()
    entry["aws"]["admin_keys"] = [f"{helper.etc}/break-glass/nobody.pub"]
    helper.request(entry)
    refused(helper, "a break-glass key of the entry: ")


def test_a_request_with_a_second_link_is_refused(helper, tmp_path):
    """A request hard-linked elsewhere is not the writer's alone: refused, whatever it holds."""
    request = helper.request()
    os.link(request, tmp_path / "second-link")
    refused(helper, "not a regular file of zab-admin's")


def test_a_withdrawn_request_is_never_taken(helper):
    """Review of #423: the writer withdraws an unanswered request by renaming it away, and the
    helper claims one by renaming it: once withdrawn, the helper finds none and says nothing."""
    request = helper.request()
    request.rename(request.with_name("request.42.withdrawn"))
    before = helper.files()
    assert helper.run() == 0
    assert helper.files() == before
    assert not (Path(helper.paths.state) / "result.json").exists()


def test_a_request_of_another_user_is_refused(helper):
    helper.request()
    helper.owner += 1  # as if zab-admin were someone else: the directory is checked first
    refused(helper, "is not zab-admin's alone")


def test_a_failure_after_the_key_removes_it(helper, monkeypatch):
    """#332: a write that fails after the key leaves admin.toml as it was, and no key."""

    def no_link(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(confighelper.os, "link", no_link)
    helper.request()
    refused(helper, "admin.toml was not written: No space left on device")


def test_a_failed_record_writes_nothing(helper):
    """The record comes before the renames: a change it can't record is not made."""
    (Path(helper.paths.state) / "changes.jsonl").mkdir()  # not a file: the append fails
    helper.request()
    before = helper.files()
    assert helper.run() == 1
    assert helper.files() == before
    assert "admin.toml was not written" in helper.result()["message"]


def test_the_new_file_must_equal_the_old_plus_the_entry(helper, monkeypatch):
    """The structural check behind the loader: a rendering that slipped in a second entry, which
    the loader alone would take, is refused."""
    real = confighelper.render
    other = helper.entry("sneaky")
    other["bot"] |= {"user_id": 904, "channels": [45], "email": "s-bot@example.zulipchat.com"}
    other["alerts_channel"] = 78

    def two(entry):
        return real(entry) + "\n" + real(other)

    monkeypatch.setattr(confighelper, "render", two)
    helper.request()
    refused(helper, "would change more than the new entry")


def test_the_entry_renders_as_toml_from_json_values(helper):
    entry = helper.entry()
    entry["github_org"] = 'a"b\\c'
    text = confighelper.render(entry)
    assert text.startswith("[[workshop]]\nname = ")
    assert tomllib.loads(text) == {"workshop": [entry]}


def test_the_command_line_names_the_helper(capsys):
    """#437: the unit runs `main` itself, never `python -m zab.admin`, whose imports would run
    as root too; `test_admin_imports.py` runs the unit's own command."""
    from zab.admin.__main__ import main as admin_main

    with pytest.raises(SystemExit) as exit_:
        confighelper.main(["--help"])
    assert exit_.value.code == 0
    assert capsys.readouterr().out.startswith("usage: zab-admin-config")
    assert admin_main(["config-helper"]) == 2  # not a command there any more
    assert "config-helper" not in capsys.readouterr().err


def test_a_refusal_is_raised_as_one_line(helper):
    with pytest.raises(Refused, match="not JSON"):
        confighelper.parse(b"\xff")


@pytest.mark.parametrize("then_full", [False, True])
def test_the_record_is_written_whole_or_not_at_all(helper, monkeypatch, then_full):
    """Codex round 2 on #421: the record's write may be short (a full disk). A short write is
    continued; one that then fails leaves the log as it was, and the change is not made."""
    log = Path(helper.paths.state) / "changes.jsonl"
    log.write_text('{"earlier": "change"}\n')
    real, calls = os.write, []

    def short(fd, data):
        calls.append(len(data))
        if len(calls) == 3:  # the key's, admin.toml's, then the record's first try
            return real(fd, bytes(data[:10]))
        if len(calls) == 4 and not then_full:
            raise OSError(28, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(confighelper.os, "write", short)
    helper.request()
    if then_full:
        assert helper.run() == 0
        earlier, line = log.read_text().splitlines()
        assert earlier == '{"earlier": "change"}'
        assert json.loads(line)["workshop"] == "mit-2027"
    else:
        before = helper.files()
        assert helper.run() == 1
        assert helper.files() == before and log.read_text() == '{"earlier": "change"}\n'


def test_the_service_runs_on_the_servers_own_paths(monkeypatch):
    """Codex round 1 on #421: the unit passes no `--config`, so the default must be the path
    itself, not the slot of `Paths.config`; and the users are looked up by name."""
    seen = {}

    def run(paths, owner, group):
        seen.update(paths=paths, owner=owner, group=group)
        return 0

    monkeypatch.setattr(confighelper, "run", run)
    monkeypatch.setattr(confighelper.pwd, "getpwnam", lambda name: type("P", (), {"pw_uid": 7}))
    monkeypatch.setattr(confighelper.grp, "getgrnam", lambda name: type("G", (), {"gr_gid": 8}))
    assert confighelper.main([]) == 0
    assert seen["paths"] == Paths() and (seen["owner"], seen["group"]) == (7, 8)
    assert seen["paths"].config == "/etc/zab-admin/admin.toml"
    assert seen["paths"].requests == "/var/lib/zab-admin-config/requests"


def test_a_key_that_fails_half_written_is_removed_and_a_retry_works(helper, monkeypatch):
    """Codex round 1 on #421: the key file is created, then its write fails (a full disk): it
    is removed, so the next request isn't refused for a key file left behind."""
    real, calls = os.fsync, []

    def full_once(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(fd)

    monkeypatch.setattr(confighelper.os, "fsync", full_once)
    helper.request()
    refused(helper, "the key was not written: No space left on device")
    helper.request()
    assert helper.run() == 0, helper.result()
    key = helper.etc / "secrets" / "bots" / "mit-2027.zuliprc"
    assert key.read_text() == ZULIPRC
