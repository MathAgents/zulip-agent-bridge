"""A24's reading of the trust fix (#992), offline: the plan's command as the bot renders it, the
rows as `trust_fix.py` prints them, and the retry of a row stopped on apt's lock, whose later
apply reads `current` what the first one changed (Codex round 1 on #994)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from admin.test_admin_actions import FLEET
from e2e.admin import test_scenarios_4_trust_fix as a24
from e2e.admin.harness.chat import Plan
from zab.admin.actions import Row, build
from zab.admin.grammar import Command, Selection
from zab.admin.planning import _step

ADMIN = Path(__file__).resolve().parents[3] / "workshop" / "admin"
if str(ADMIN) not in sys.path:
    sys.path.insert(0, str(ADMIN))

import trust_fix  # noqa: E402

B, HOST = a24.B, "198.51.100.2"
#: zab-test-b's dry run once its key is out (A24's `UNSET`): uv older, step 5 with its work.
UNSET_DRY = {"uv": "0.13.2: would install 0.13.7", "claude store": "unset: would set system"}
#: The same VM as A4's first boot leaves it (#885): the key there, uv still older.
FIRST_BOOT_DRY = {"uv": "0.13.2: would install 0.13.7", "claude store": "system, current"}
LOCKED = "FAILED: apt-get update: E: Could not get lock /var/lib/apt/lists/lock"
BLOCKED = "blocked, nothing installed: /etc/ssl/certs still holds a distrusted root"


def command(slug: str = B) -> str:
    """The plan's one command, as the bot renders it for `!trust-fix <slug>`."""
    fleet = dataclasses.replace(FLEET, rows=(Row(slug, HOST, "us-west-2"),))
    action = build(Command("trust-fix", selection=Selection(slugs=(slug,))), fleet)
    (step,) = action.applied
    return _step(step.argv, step.env)


def printed(steps: dict[str, str], ok: bool, *, apply: bool = True) -> str:
    """trust_fix.py's table of zab-test-b, as it prints it, its step's frame around it."""
    result = trust_fix.FixResult(B, HOST, report={"apply": apply, "steps": steps, "ok": ok})
    table = trust_fix.format_table(trust_fix.COLUMNS, [result.row()])
    return f"── {a24.SCRIPT}\n{table}\n\nsummary\n── {a24.SCRIPT} exited {int(not ok)}\n"


class Talk:
    """The bot, as A24 talks to it: each `!trust-fix` planned, each approval answered with the
    next of `results`."""

    def __init__(self, results: list[str]) -> None:
        self.results, self.sent = list(results), []

    async def request(self, text: str) -> Plan:
        self.sent.append(text)
        dry = printed({"claude store": "system, current"}, True, apply=False)
        return Plan("P-9", "zab-admin-e2e", text, "!approve P-9 abcdef", "abcdef", dry,
                    targets=[B], commands=[command()])  # fmt: skip

    async def approve(self, plan: Plan) -> tuple[dict, str]:
        return {}, self.results.pop(0)


def test_the_plans_command_is_the_apply_as_the_bot_renders_it():
    """The plan's **Commands** shows the apply's list (`Action.applied`'s `argv`), with
    `--admin-bot`, ending `--apply`."""
    talk = Talk([])
    plan = asyncio.run(a24.plan_of(talk, f"!trust-fix {B}", [B]))
    assert plan.commands[0].endswith(f"--admin-bot --only {B} --apply"), plan.commands


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(a24, "AGAIN_S", 0.0)
    return SimpleNamespace(notes=[])


def test_a_row_stopped_on_apts_lock_is_sent_again_and_the_first_apply_is_checked(quick):
    """The first apply sets Claude Code's store whatever apt does (`claude store` never waits:
    `trust_fix_remote.run_steps`); its retry reads it `system, current`. So the store's change is
    checked on the first apply's row, the fix's success on the last."""
    first = a24.rows(printed({"ca-certificates": LOCKED, "uv": BLOCKED,
                              "claude store": "unset -> system"}, False))[B]  # fmt: skip
    again = printed({"ca-certificates": "20260601 -> 20260816", "uv": "0.13.2 -> 0.13.7",
                     "claude store": "system, current"}, True)  # fmt: skip
    talk = Talk([again])
    last = asyncio.run(a24.until_fixed(talk, quick, first))
    assert talk.sent == [f"!trust-fix {B}"] and last["check"] == "ok", last
    assert last["claude store"] == "system, current"  # why the first apply's row is checked
    store = a24.planned(a24.rows(printed(UNSET_DRY, False, apply=False))[B])
    a24.fixed_rows(first, last, store=store)
    with pytest.raises(AssertionError, match="the first apply's row"):
        a24.fixed_rows(last, last, store=store)


def test_a_row_done_or_failed_otherwise_is_never_sent_again(quick):
    done = a24.rows(printed({"claude store": "unset -> system"}, True))[B]
    talk = Talk([])
    assert asyncio.run(a24.until_fixed(talk, quick, done)) == done and talk.sent == []
    failed = a24.rows(printed({"conda": "FAILED: base: solver", "claude store": "x"}, False))[B]
    assert asyncio.run(a24.until_fixed(talk, quick, failed)) == failed and talk.sent == []
    with pytest.raises(AssertionError, match="row after the apply"):
        a24.fixed_rows(done, failed, store="unset -> system")


def test_a_lock_that_outlasts_every_try_ends_on_the_last_row(quick):
    locked = printed({"ca-certificates": LOCKED, "claude store": "system, current"}, False)
    first = a24.rows(locked)[B]
    talk = Talk([locked] * a24.TRIES)
    last = asyncio.run(a24.until_fixed(talk, quick, first))
    assert len(talk.sent) == a24.TRIES and last["check"] == "FAILED"
    with pytest.raises(AssertionError, match="row after the apply"):
        a24.fixed_rows(first, last, store="system, current")


@pytest.mark.parametrize(
    "cell, after",
    [
        ("unset: would set system", "unset -> system"),
        ("absent: would set system", "absent -> system"),
        ("bundled,system: would set system", "bundled,system -> system"),
        ("system, current", "system, current"),
    ],
)
def test_the_first_applys_store_follows_from_the_dry_runs_cell(cell, after):
    """Review round 1 on #994, B1: the transition A24 expects is the one its dry run's cell
    names (`trust_fix_remote.step_claude`), never a fixed one."""
    assert a24.store_after(cell) == after
    with pytest.raises(AssertionError, match="no apply sets"):
        a24.store_after("FAILED: /etc/claude-code/managed-settings.json left as it is: x")


def test_a_dry_run_whose_store_is_set_already_is_said_so():
    """Review round 1 on #994, B1: on a VM as A4's first boot leaves it (the key written since
    #885), the row reads `system, current` with `check` `FAILED` (uv is older): step 5 has
    nothing to do, so A24 takes the key out first (`UNSET`), and a row that still reads so says
    the removal didn't hold, naming step 5, not a mismatch further on."""
    first_boot = a24.rows(printed(FIRST_BOOT_DRY, False, apply=False))[B]
    assert first_boot["check"] == "FAILED" and first_boot["claude store"] == "system, current"
    assert a24.store_after(first_boot["claude store"]) == "system, current"
    with pytest.raises(AssertionError, match="step 5 has nothing to do on zab-test-b"):
        a24.planned(first_boot)
    unset = a24.rows(printed(UNSET_DRY, False, apply=False))[B]
    assert a24.planned(unset) == "unset -> system"
    ok = a24.rows(printed({"claude store": "system, current"}, True, apply=False))[B]
    with pytest.raises(AssertionError, match="dry run"):
        a24.planned(ok)


def test_unset_takes_the_key_out_and_keeps_the_rest(tmp_path):
    """A24's `UNSET`, run here on a copy of the managed settings as the first boot writes them:
    the key goes, every other key and the file's mode stay."""
    path = tmp_path / "managed-settings.json"
    path.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_CERT_STORE":
                                        "system"}, "disableClaudeAiConnectors": True}))  # fmt: skip
    path.chmod(0o644)
    code = a24.UNSET.replace(a24.MANAGED, str(path))
    assert code != a24.UNSET  # it names the file A24 reads
    done = subprocess.run(  # noqa: S603 - this interpreter, A24's own literal
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    left = {"env": {"DISABLE_AUTOUPDATER": "1"}, "disableClaudeAiConnectors": True}
    assert json.loads(path.read_text()) == left == json.loads(done.stdout)
    assert os.stat(path).st_mode & 0o777 == 0o644
