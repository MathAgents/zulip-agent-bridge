"""`ClaudeBackend.login` (issue #36 `!login`): `claude auth login` in a pseudo-terminal, against
the fake `claude`, which prints what the real CLI prints (2.1.282 to 2.1.289)."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from backends.claude_backend_helpers import calls, make_backend
from zab.backends import claude_login as cl

pytestmark = pytest.mark.integration

GOOD = "fake-code#fake-state"


class Dm:
    """The owner's DM: what the bot sent, and the owner's scripted replies."""

    def __init__(self, *replies: str | None) -> None:
        self.sent: list[str] = []
        self.replies = list(replies)
        self.timeouts: list[float] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def receive(self, timeout_s: float) -> str | None:
        self.timeouts.append(timeout_s)
        return self.replies.pop(0) if self.replies else None


@pytest.fixture
def signed_out(home, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGGED_IN", "0")
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_LOGIN", raising=False)
    return home


async def status(backend) -> dict:
    checks = await backend.doctor()
    return {c.name: c.ok for c in checks}


async def test_login_sends_the_url_takes_the_code_and_confirms(signed_out, caplog):
    caplog.set_level(logging.DEBUG)
    backend = make_backend(signed_out)
    assert (await status(backend))["claude account test"] is False
    dm = Dm(f"  `{GOOD}`  ")  # pasted with stray backticks and spaces
    assert await asyncio.wait_for(backend.login(None, dm.send, dm.receive), 30) is True
    assert len(dm.sent) == 1 and cl.INSTRUCTIONS in dm.sent[0]
    url = dm.sent[0].splitlines()[-1]
    assert url == "https://claude.com/cai/oauth/authorize?" + url.split("?", 1)[1]
    assert "\x1b" not in dm.sent[0]  # the OSC 8 wrapper is gone
    assert dm.timeouts and 590 < dm.timeouts[0] <= 600
    assert (await status(backend))["claude account test"] is True
    login = next(c for c in calls(signed_out) if c["argv"][:2] == ["auth", "login"])
    assert login["env"]["CLAUDE_CONFIG_DIR"] == str(signed_out["account"])
    assert "ANTHROPIC_API_KEY" not in login["env"]  # the scrubbed turn environment
    assert GOOD not in caplog.text and GOOD not in json.dumps(calls(signed_out))


async def test_a_bad_code_fails_cleanly_and_never_echoes_the_code(signed_out, caplog):
    caplog.set_level(logging.DEBUG)
    backend = make_backend(signed_out)
    wrong = "wrong-code#not-it"
    dm = Dm(wrong)
    assert await asyncio.wait_for(backend.login("test", dm.send, dm.receive), 30) is False
    assert "Sign-in failed: Login failed: Request failed with status code 400" in dm.sent[-1]
    assert all(wrong not in m for m in dm.sent) and wrong not in caplog.text
    assert (await status(backend))["claude account test"] is False


async def test_timeout_kills_the_cli_and_says_so(signed_out, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGIN", "hang")
    backend = make_backend(signed_out)
    started: list = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kw):
        proc = await real(*args, **kw)
        started.append(proc)
        return proc

    monkeypatch.setattr(cl.asyncio, "create_subprocess_exec", spy)
    dm = Dm()  # the owner never answers
    assert await asyncio.wait_for(backend.login(None, dm.send, dm.receive), 30) is False
    assert "timed out" in dm.sent[-1]
    assert started and started[0].returncode is not None  # the group was killed


async def test_no_url_is_a_clear_failure(signed_out, monkeypatch):
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGIN", "nourl")
    monkeypatch.setattr(cl, "URL_TIMEOUT_S", 1.0)
    dm = Dm(GOOD)
    backend = make_backend(signed_out)
    assert await asyncio.wait_for(backend.login(None, dm.send, dm.receive), 30) is False
    assert dm.sent == ["Claude did not show a sign-in link; try `!login` again."]
    assert dm.timeouts == []  # nothing was asked of the owner


async def test_the_default_account_leaves_claude_config_dir_unset(tmp_path, home, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGGED_IN", "0")
    monkeypatch.delenv("ZAB_FAKE_CLAUDE_LOGIN", raising=False)
    from zab.backends import claude as cb
    from zab.core.types import SessionSettings

    backend = cb.ClaudeBackend(
        "claude",
        cb.ClaudeConfig(
            binary=make_backend(home).config.binary,
            accounts={"main": "~/.claude"},
            default_account="main",
            defaults=SessionSettings(),
        ),
    )
    dm = Dm(GOOD)
    assert await asyncio.wait_for(backend.login(None, dm.send, dm.receive), 30) is True
    login = next(c for c in calls(home) if c["argv"][:2] == ["auth", "login"])
    assert login["env"]["CLAUDE_CONFIG_DIR"] == "<unset>"
    assert (tmp_path / "h" / ".claude" / ".fake-logged-in").exists()


def test_url_extraction_and_failure_reason():
    real = (
        "Opening browser to sign in…\r\nIf the browser didn't open, visit: "
        "\x1b]8;;https://claude.com/cai/oauth/authorize?code=true&state=abc\x07\x1b[94m"
        "https://claude.com/cai/oauth/authorize?code=true&state=abc\x1b[39m\x1b]8;;\x07\r\n"
        "Paste code here if prompted > "
    )
    assert cl.extract_url(real) == "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    plain = "visit: https://claude.com/cai/oauth/authorize?code=true&state=x\r\n"
    assert cl.extract_url(plain) == "https://claude.com/cai/oauth/authorize?code=true&state=x"
    assert cl.extract_url("Opening browser to sign in…") is None
    echoed = "SECRET#1\r\nLogin failed: Request failed with status code 400\r\n\x1b[?25h"
    assert cl._failure_reason(echoed, "SECRET#1") == (
        "Login failed: Request failed with status code 400"
    )
    # #661: redacted before the cut at 200, which a token crossing it would survive in part.
    # (The line is one line, `Login failed…`: a zuliprc `key=` line cannot be in it.)
    token = "ghp_" + "Z9y8X7w6" * 4 + "V5u4"
    reason = cl._failure_reason("Login failed: " + "w" * 180 + " " + token + "\r\n", "")
    assert "ghp_" not in reason and len(reason) <= 200
    assert cl.clean_code("  `abc#def`\n") == "abc#def"


async def test_the_cli_exiting_before_the_code_is_a_failure_not_a_crash(signed_out, monkeypatch):
    real = cl.os.write

    def gone(fd, data):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(cl.os, "write", gone)
    dm = Dm(GOOD)
    backend = make_backend(signed_out)
    try:
        assert await asyncio.wait_for(backend.login(None, dm.send, dm.receive), 30) is False
    finally:
        monkeypatch.setattr(cl.os, "write", real)
    assert "ended before the code arrived" in dm.sent[-1]


async def test_a_url_split_across_terminal_reads_is_sent_whole(signed_out, monkeypatch):
    """Codex review on #89: a read can end inside the URL; only the complete link is sent."""
    monkeypatch.setenv("ZAB_FAKE_CLAUDE_LOGIN", "split")
    dm = Dm(GOOD)
    backend = make_backend(signed_out)
    assert await asyncio.wait_for(backend.login(None, dm.send, dm.receive), 30) is True
    assert dm.sent[0].splitlines()[-1].endswith("&state=fake-state")


def test_partial_urls_are_not_taken():
    head = "visit: \x1b]8;;https://claude.com/cai/oauth/authorize?code=true&client_id="
    assert cl.extract_url(head) is None  # OSC 8 without its terminator
    assert cl.extract_url("visit: https://claude.com/cai/oauth/authorize?code=true&client") is None
    assert cl.extract_url("visit: https://claude.com/cai/oauth/authorize?a=1\r\n") == (
        "https://claude.com/cai/oauth/authorize?a=1"
    )


async def test_the_success_check_stays_strict(signed_out):
    """`auth status` exits 1 when signed out; the doctor reads that JSON, but login's own
    confirmation only accepts exit 0 with `loggedIn` true."""
    from zab.backends import claude as cb

    backend = make_backend(signed_out)
    env = cb.turn_env(str(signed_out["account"]))
    assert await cl.logged_in(backend.config.binary, env, cb._run_capture) is False


async def test_a_missing_cli_is_unavailable_not_a_failed_sign_in(signed_out, tmp_path):
    """Claude's not-ready state: the binary isn't found (yet). Login raises BackendUnavailable
    before sending anything, which the core's !login waits out and reports as such."""
    from zab.core.types import BackendUnavailable

    backend = make_backend(signed_out)
    backend.config.binary = str(tmp_path / "missing" / "claude")
    dm = Dm(GOOD)
    with pytest.raises(BackendUnavailable):
        await asyncio.wait_for(backend.login(None, dm.send, dm.receive), 30)
    assert dm.sent == [] and dm.timeouts == []
