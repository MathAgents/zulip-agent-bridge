"""A malformed zuliprc is named, never quoted (#527).

configparser's errors quote the offending line, which may be `key=…`. `ZulipClient.from_zuliprc`
says only the file and what failed, and each entry point that reads a zuliprc through it says
that and no more: the bridge's start (`zab run`), `zab init`, `zab doctor`, `zab sentinel`, and
the waker in either mode, in their output, their tracebacks and their logs. The zuliprcs are
decoys in a tmp dir, never a real key (CONTRIBUTING.md, rule 2).
"""

from __future__ import annotations

import asyncio
import base64
import configparser
import logging
import os
import subprocess
import sys
import traceback
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from zab.config import BotConfig
from zab.waker import __main__ as waker
from zab.zulip.client import ZulipClient, ZuliprcError, error_kind

pytestmark = pytest.mark.integration

DECOY = "DECOY-SECRET-527"  # a decoy, never a real key
#: The `key <32 alphanumerics>` form: no `=`, so the admin scrubber's patterns miss it (#525).
ALNUM = "Decoy527" * 4
SECRETS = (DECOY, ALNUM)
SITE, EMAIL = "https://zulip.example.org", "p@example.org"
GOOD = f"[api]\nemail={EMAIL}\nkey={'K' * 32}\nsite={SITE}\n"
#: A zuliprc with a decoy on the line configparser can't read, and the type of its error.
MALFORMED = {
    "before [api]": (f"key={DECOY}\n[api]\nemail={EMAIL}\nsite={SITE}\n".encode(),
                     "MissingSectionHeaderError"),
    "no =": (f"[api]\nemail={EMAIL}\nsite={SITE}\nkey {ALNUM}\n".encode(), "ParsingError"),
    "no name": (f"[api]\nemail={EMAIL}\nsite={SITE}\n=key={DECOY}\n".encode(), "ParsingError"),
    "twice": (f"[api]\nemail={EMAIL}\nsite={SITE}\nkey={DECOY}\nkey={ALNUM}\n".encode(),
              "DuplicateOptionError"),
    "not UTF-8": (f"[api]\nemail={EMAIL}\nsite={SITE}\nkey={DECOY}".encode() + b"\xff\n",
                  "UnicodeDecodeError"),
}  # fmt: skip
INSTANCE = "i-0aaaaaaaaaaaaaaa1"


@pytest.fixture(params=sorted(MALFORMED))
def bad(request, tmp_path) -> tuple[Path, str]:
    """A malformed decoy zuliprc in a tmp dir, 0600 as zab requires, and its error's type."""
    data, why = MALFORMED[request.param]
    path = tmp_path / "bot.zuliprc"
    path.write_bytes(data)
    path.chmod(0o600)
    return path, why


def shown(exc: BaseException) -> str:
    """Everything a traceback of `exc` prints, its chain included."""
    return "".join(traceback.format_exception(exc))


def clean(text: str) -> bool:
    return not any(secret in text for secret in SECRETS)


# ------------------------------------------------------------------------------ from_zuliprc


def test_the_decoys_are_what_configparser_would_have_shown(bad):
    """The risk, on main before #527: configparser's message quotes the line (a decoding error
    keeps the bytes, in `object`)."""
    path, why = bad
    with pytest.raises((configparser.Error, UnicodeDecodeError)) as raw:
        configparser.ConfigParser(interpolation=None).read_string(path.read_bytes().decode())
    assert type(raw.value).__name__ == why
    held = raw.value.object.decode("utf-8", "replace") if why == "UnicodeDecodeError" else ""
    assert why == "DuplicateOptionError" or not clean(str(raw.value) + held)


def test_a_malformed_zuliprc_is_named_by_its_type_and_chains_nothing(bad):
    path, why = bad
    with pytest.raises(ZuliprcError) as caught:
        ZulipClient.from_zuliprc(path)
    exc = caught.value
    assert isinstance(exc, ValueError)  # what the callers catch, as the HTTPS refusal
    assert (str(exc), exc.path, exc.why, error_kind(exc)) == (f"{path}: {why}", str(path), why, why)
    assert exc.__cause__ is None and exc.__context__ is None and exc.__suppress_context__
    assert clean(shown(exc))


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("", "no [api] section"),
        (f"[apl]\nkey={DECOY}\n", "no [api] section"),
        (f"[api]\nemail={EMAIL}\nsite={SITE}\n[other]\nkey={DECOY}\n", "no key in [api]"),
        (f"[api]\nkey={DECOY}\nsite={SITE}\n", "no email in [api]"),
        (f"[api]\nkey={DECOY}\nemail={EMAIL}\n", "no site in [api]"),
    ],
    ids=["empty", "no [api]", "no key", "no email", "no site"],
)
def test_a_zuliprc_without_a_part_says_which(tmp_path, text, said):
    path = tmp_path / "bot.zuliprc"
    path.write_text(text)
    with pytest.raises(ZuliprcError) as caught:
        ZulipClient.from_zuliprc(path)
    assert str(caught.value) == f"{path}: {said}" and caught.value.__context__ is None
    assert clean(shown(caught.value))


def before(path: str | Path, **kwargs: Any) -> ZulipClient:
    """`from_zuliprc` as it was before #527, verbatim."""
    config = configparser.ConfigParser(interpolation=None)
    with Path(path).expanduser().open() as stream:
        config.read_file(stream)
    api = config["api"]
    return ZulipClient(api["site"], api["email"], api["key"], **kwargs)


VALID = {
    "plain": GOOD,
    "a trailing slash": GOOD.replace(SITE, SITE + "/"),
    "colons and case": f"[api]\nEMAIL: {EMAIL}\nKey : {'k' * 32}\nsite:{SITE}\n",
    "comments and sections": f"# zab\n[other]\nkey=x\n[api]\n; the bot's\nemail = {EMAIL}\n"
    f"key = {'K' * 32}\nsite = {SITE}\n",
    "the site in DEFAULT": f"[DEFAULT]\nsite={SITE}\n[api]\nemail={EMAIL}\nkey={'K' * 32}\n",
    "a % in the key": f"[api]\nemail={EMAIL}\nkey=K%(x)s{'K' * 27}\nsite={SITE}\n",
}


@pytest.mark.parametrize("name", sorted(VALID))
async def test_a_valid_zuliprc_loads_exactly_as_before(tmp_path, monkeypatch, name):
    """The same site, request URL and credentials as the implementation before #527, by `~`
    too; a missing file is still the OSError and an HTTP site the ValueError."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "bot.zuliprc").write_text(VALID[name])
    seen: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["Authorization"]))
        return httpx.Response(200, json={"result": "success", "user_id": 1})

    transport = httpx.MockTransport(answer)
    for path in (tmp_path / "bot.zuliprc", "~/bot.zuliprc"):
        for load in (before, ZulipClient.from_zuliprc):
            client = load(path, transport=transport)
            try:
                assert client.site == SITE
                await client.request("GET", "users/me")
            finally:
                await client.close()
    assert len(seen) == 4 and len(set(seen)) == 1
    url, auth = seen[0]
    assert url == f"{SITE}/api/v1/users/me" and auth.startswith("Basic ")
    assert base64.b64decode(auth.removeprefix("Basic ")).decode().startswith(f"{EMAIL}:")
    for load in (before, ZulipClient.from_zuliprc):
        with pytest.raises(FileNotFoundError):
            load(tmp_path / "missing.zuliprc")
    (tmp_path / "bot.zuliprc").write_text(GOOD.replace("https://", "http://"))
    for load in (before, ZulipClient.from_zuliprc):
        with pytest.raises(ValueError) as refused:
            load(tmp_path / "bot.zuliprc")
        assert str(refused.value) == "Zulip site must use HTTPS"
        assert not isinstance(refused.value, ZuliprcError)


def test_error_kind_is_the_type_for_anything_else():
    assert error_kind(KeyError("site")) == "KeyError"
    assert error_kind(ZuliprcError("/x.zuliprc", "ParsingError")) == "ParsingError"


# --------------------------------------------------------------------- the zab entry points


def config(tmp_path: Path, zuliprc: Path) -> Path:
    """A participant's config: one bot on a Claude backend whose binary is absent, so that the
    bridge's start reaches the bot's zuliprc offline, with no agent and no credential."""
    (tmp_path / "home").mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        f'[bridge]\nstate_dir = "{tmp_path / "state"}"\ndefault_project = "{tmp_path}"\n\n'
        f'[[bots]]\nname = "dev"\nzuliprc = "{zuliprc}"\nbackend = "claude"\n'
        "allow_user_ids = [20]\n\n"
        f'[backends.claude]\nkind = "claude"\nbinary = "{tmp_path / "absent" / "claude"}"\n'
        f'accounts = {{ main = "{tmp_path / "home" / ".claude"}" }}\n'
    )
    return path


def zab(tmp_path: Path, *argv: str | Path) -> tuple[int, str]:
    """`zab *argv` as a service runs it, its stdout and stderr merged as the journal merges
    them, in its own environment and HOME."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "home"),
           "LANG": "C.UTF-8"}  # fmt: skip
    done = subprocess.run(  # noqa: S603 - an argument list
        [sys.executable, "-m", "zab", *map(str, argv)], env=env, cwd=tmp_path,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
        timeout=60, check=False,
    )  # fmt: skip
    assert clean(done.stdout), done.stdout
    return done.returncode, done.stdout


def logs_are_clean(directory: Path) -> bool:
    """No file under `directory` (the bridge's state dir: its logs) holds a decoy."""
    return all(clean(p.read_bytes().decode("utf-8", "replace")) for p in directory.rglob("*")
               if p.is_file())  # fmt: skip


def test_the_bridges_start(bad, tmp_path):
    """`zab run`, the participant's zab.service: one line, as before #527, and clean logs."""
    path, why = bad
    code, out = zab(tmp_path, "run", "--config", config(tmp_path, path))
    assert code == 1 and out.splitlines()[-1] == f"zab run failed ({why})"
    assert "Traceback" not in out and (tmp_path / "state" / "logs" / "zab.log").is_file()
    assert logs_are_clean(tmp_path / "state")


def test_zab_init(bad, tmp_path):
    path, why = bad
    target = tmp_path / "new" / "config.toml"
    argv = ["init", "--config", target, "--bot", "dev", "--zuliprc", path, "--backend", "codex"]
    assert zab(tmp_path, *argv) == (1, f"zab init failed ({why})\n")
    assert not target.exists()


def test_zab_doctor(bad, tmp_path):
    path, why = bad
    code, out = zab(tmp_path, "doctor", "--config", config(tmp_path, path))
    assert code != 0 and f"FAIL dev:connection: {why}" in out.splitlines()
    assert "Traceback" not in out


def test_zab_sentinel(bad, tmp_path):
    """No handler: the traceback names the file and the type, and chains nothing."""
    path, why = bad
    code, out = zab(tmp_path, "sentinel", "--config", config(tmp_path, path))
    assert code == 1 and out.splitlines()[-1] == f"zab.zulip.client.ZuliprcError: {path}: {why}"
    assert "During handling" not in out and "direct cause" not in out


# ------------------------------------------------------------------------------- the waker


def test_the_wakers_direct_mode(bad, tmp_path, monkeypatch, caplog):
    """The files host's waker (#207): no handler, so systemd's journal gets the traceback, which
    names the file and the type and chains nothing. boto3 is a stand-in: no AWS call is made."""
    path, why = bad
    caplog.set_level(logging.INFO)
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: types.SimpleNamespace(close=lambda: None)
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    settings = {"organizers_channel": 42, "state_dir": str(tmp_path / "state"),
                "bots": [{"instance_id": INSTANCE, "region": "us-east-2"}]}  # fmt: skip
    entries = [(BotConfig(name="codex-a", zuliprc=str(path)), INSTANCE)]
    monkeypatch.setattr(waker, "load", lambda _: (settings, entries))
    monkeypatch.setattr(waker, "setup_logging", lambda: None)
    with pytest.raises(ZuliprcError) as caught:
        waker.main(["run", "--config", str(tmp_path / "config.toml")])
    assert str(caught.value) == f"{path}: {why}" and caught.value.__context__ is None
    assert clean(shown(caught.value)) and clean(caplog.text)


def test_the_wakers_starter_mode(bad, tmp_path, monkeypatch, caplog):
    """The admin server's waker: its supervisor logs the error's kind, as before #527, and
    retries; a run is cut short here once it has tried twice."""
    path, why = bad
    monkeypatch.setattr(waker, "RETRY", 0.01)
    tried: list[str] = []

    async def serve(makers, path, config, entries, store):
        ((name, make),) = makers
        twice = asyncio.Event()

        def counted():
            tried.append(name)
            if len(tried) >= 2:
                twice.set()
            return make()

        task = asyncio.ensure_future(waker.supervise(name, counted))
        try:
            await asyncio.wait_for(twice.wait(), 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(waker, "serve", serve)
    fleets = [waker.Fleet("alpha", 42, [(BotConfig(name="codex-alice", zuliprc=str(path)),
                                         INSTANCE)])]  # fmt: skip
    conf = tmp_path / "config.toml"
    conf.write_text("unchanged")
    waker.run_fleets(conf, {"starter": str(tmp_path / "start.sock"), "fleets": fleets}, [], None)
    assert len(tried) >= 2
    assert caplog.text.count(f"Workshop alpha: no waker ({why}); retrying with a backoff") == 1
    assert clean(caplog.text)
