import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from zab.core.types import (
    ItemCompleted,
    ItemKind,
    ItemStarted,
    ItemStatus,
    TurnCompleted,
    TurnStatus,
)
from zab.testing.fake_codex import FakeCodex

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/codex/0.157.1"
SCHEMAS = ROOT / "docs/protocol/codex-0.157.1"


def test_every_recorded_message_matches_installed_schema():
    validators = {
        name: Draft7Validator(json.loads((SCHEMAS / f"{name}.json").read_text()))
        for name in [
            "ClientRequest",
            "ClientNotification",
            "ServerRequest",
            "ServerNotification",
            "JSONRPCMessage",
        ]
    }
    checked = total = 0
    for fixture in sorted(FIXTURES.glob("*.jsonl")):
        calls = {}
        for row in FakeCodex(fixture).records:
            if "message" not in row:
                continue
            total += 1
            message = row["message"]
            if "method" in message:
                side = "Client" if row["direction"] == "send" else "Server"
                kind = "Request" if "id" in message else "Notification"
                validators[side + kind].validate(message)
                if row["direction"] == "send" and "id" in message:
                    calls[message["id"]] = message["method"]
            else:
                validators["JSONRPCMessage"].validate(message)
                if row["direction"] == "receive" and "result" in message:
                    name = "".join(p.title() for p in calls[message["id"]].split("/"))
                    schema = SCHEMAS / "v2" / f"{name}Response.json"
                    if not schema.exists():
                        schema = next(SCHEMAS.rglob(f"{name}Response.json"))
                    Draft7Validator(json.loads(schema.read_text())).validate(message["result"])
            checked += 1
    assert checked == total and checked > 100


@pytest.mark.parametrize(
    "scenario", ["simple", "tool_use", "approval", "steer", "interrupt", "resume"]
)
async def test_recorded_turn_replays_to_completion(scenario):
    fake = FakeCodex(FIXTURES / f"{scenario}.jsonl")
    events = [event async for event in fake.events()]
    finals = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(finals) == 1
    expected = TurnStatus.INTERRUPTED if scenario == "interrupt" else TurnStatus.COMPLETED
    assert finals[0].status == expected
    if scenario in ("simple", "resume"):
        assert finals[0].final_text == "FIXTURE_OK"
    if scenario == "steer":
        assert "STEER_FIXTURE_OK" in finals[0].final_text
        assert any(
            isinstance(e, ItemCompleted)
            and e.item.kind == ItemKind.USER_MESSAGE
            and "STEER_FIXTURE_OK" in (e.item.text or "")
            for e in events
        )
    if scenario == "approval":
        messages = [r["message"] for r in fake.records if "message" in r]
        assert any(m.get("method") == "item/commandExecution/requestApproval" for m in messages)
        assert any(m.get("result", {}).get("decision") == "decline" for m in messages)


async def test_codex_reports_a_steer_when_it_takes_it_in():
    """#654: a steer's `userMessage` item comes when Codex takes the input in, not with the
    `turn/steer` response: after the command that was running completes, 2.9 s later here. It
    maps to the steer's `ItemCompleted(USER_MESSAGE)`, on which the core marks the message
    read. The running command is `IN_PROGRESS` from its `item/started`."""
    fake = FakeCodex(FIXTURES / "steer.jsonl")
    rows = [r for r in fake.records if "message" in r]
    steer = next(r["message"] for r in rows if r["message"].get("method") == "turn/steer")
    client = steer["params"]["clientUserMessageId"]

    def when(test) -> float:
        return next(r["t"] for r in rows if r["direction"] == "receive" and test(r["message"]))

    def item(message: dict, kind: str) -> dict | None:
        if message.get("method") == f"item/{kind}":
            return message["params"]["item"]
        return None

    answered = when(lambda m: m.get("id") == steer["id"])
    ran = when(lambda m: (item(m, "completed") or {}).get("type") == "commandExecution")
    taken = when(lambda m: (item(m, "completed") or {}).get("clientId") == client)
    assert answered < ran <= taken and taken - answered > 2
    events = [event async for event in fake.events()]
    (echo,) = [e for e in events if isinstance(e, ItemCompleted) and e.item.client_id == client]
    assert echo.item.kind == ItemKind.USER_MESSAGE
    (command,) = [
        e for e in events if isinstance(e, ItemStarted) and e.item.kind == ItemKind.COMMAND
    ]
    assert command.item.status == ItemStatus.IN_PROGRESS
