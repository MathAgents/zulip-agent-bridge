"""`!tools` (#475): the VM's software from `/etc/zab/tools.json`, answered by the bridge."""

from __future__ import annotations

import json
import os
import re
import threading

import pytest
from test_bridge import bridge as bridge
from test_bridge import user_message

from zab.core import software
from zab.core.commands import DM_HELP, HELP
from zab.render.mentions import ZWSP

pytestmark = pytest.mark.integration

M2 = {
    "name": "Macaulay2",
    "version": "1.26.05",
    "area": "Commutative algebra and algebraic geometry",
    "call": "M2 --script file.m2",
    "summary": "one short line",
}


def manifest(*tools, **top) -> str:
    return json.dumps({"schema": 1, "image": "zab-fleet-x", "built": "2026-10-20", **top,
                       "tools": list(tools)})  # fmt: skip


def write(tmp_path, text: str | bytes) -> str:
    path = tmp_path / "tools.json"
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return str(path)


def test_the_reply_groups_by_area_in_first_seen_order_with_version_and_call():
    gp = {"name": "PARI/GP", "version": "2.17.4", "area": "Number theory", "call": "gp -q f.gp"}
    magma = {"name": "Magma", "area": "Number theory", "call": "magma -b f.m", "unverified": True}
    loose = {"name": "jq", "call": "jq . f.json"}  # no area: under "Other", last
    sing = {**M2, "name": "Singular", "version": "", "call": "Singular -q f.sing"}
    m = software.parse(manifest(gp, loose, M2, magma, sing))
    text = software.render(m, 10_000)
    assert text.splitlines() == [
        "**Software on this VM** (image zab-fleet-x, built 2026-10-20)",
        "",
        "**Number theory**",
        "- **PARI/GP** 2.17.4 · `gp -q f.gp`",
        "- **Magma** · `magma -b f.m` (unverified)",
        "",
        "**Commutative algebra and algebraic geometry**",
        "- **Macaulay2** 1.26.05 · `M2 --script file.m2` · one short line",
        "- **Singular** · `Singular -q f.sing` · one short line",
        "",
        "**Other**",
        "- **jq** · `jq . f.json`",
        "",
        software.FOOTER,
    ]


def test_bad_entries_are_skipped_unknown_keys_ignored_and_fields_kept_to_one_line():
    m = software.parse(
        manifest(
            "Lean",
            {"version": "4"},
            {"name": "  "},
            {"name": 7},
            {**M2, "summary": "two\nlines\tand  spaces", "extra": {"x": 1}, "version": 126},
            {"name": "x" * 200, "unverified": "yes"},  # only `true` marks an entry
        )
    )
    assert [t.name for t in m.tools] == ["Macaulay2", "x" * 59 + "…"]
    assert m.tools[0].summary == "two lines and spaces" and m.tools[0].version == ""
    assert not m.tools[1].unverified
    assert software.parse('{"schema": 1, "tools": []}') == software.Manifest("", "", ())


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{",
        "[]",
        '{"tools": []}',  # no schema
        '{"schema": 2, "tools": []}',  # a later schema is not guessed at
        '{"schema": "1", "tools": []}',
        '{"schema": true, "tools": []}',
        '{"schema": 1}',
        '{"schema": 1, "tools": {}}',
        '{"schema": 1, "tools": ' + "[" * 200_000 + "]" * 200_000 + "}",  # RecursionError
    ],
)
def test_anything_but_schema_1_is_unreadable(text):
    with pytest.raises(software.ManifestError):
        software.parse(text)


def test_call_with_backticks_and_mentions_in_the_text_are_harmless():
    tool = {"name": "@**all**", "call": "echo `date`", "summary": "pings @**Drew** and @*team*"}
    text = software.render(software.parse(manifest(tool)), 10_000)
    assert " · `` echo `date` `` · " in text  # a fence of two, padded: it ends in a backtick
    assert "@**" not in text and "@*team*" not in text and f"@{ZWSP}**Drew**" in text


def test_code_spans_take_a_fence_no_backtick_run_in_the_call_matches():
    assert software.code("gp -q") == "`gp -q`"
    assert software.code("a `b` c") == "``a `b` c``"
    assert software.code("`x`") == "`` `x` ``"
    assert software.code("a `` b ` c") == "```a `` b ` c```"


def test_over_the_limit_summaries_go_first_then_tools_with_a_pointer():
    tools = [{**M2, "name": f"Tool{i:02}", "area": f"Area {i % 3}"} for i in range(30)]
    m = software.parse(manifest(*tools))
    full = software.render(m, 100_000)
    assert full.count("one short line") == 30
    lean = software.render(m, len(full) - 1)
    assert "one short line" not in lean and lean.count("- **Tool") == 30
    cut = software.render(m, len(lean) - 1)
    assert len(cut) <= len(lean) - 1
    shown = cut.count("- **Tool")
    assert 0 < shown < 30
    assert f"… and {30 - shown} more: ask an agent to show you the whole list, " in cut
    assert "`/etc/zab/tools.json`" in cut and cut.endswith(software.FOOTER)
    # the most that fit: one more would not
    assert len(software._compose(m, shown + 1, summary=False)) > len(lean) - 1
    # in the reply's order: Area 0's first, the last area's last ones are the ones left out
    assert "**Area 0**" in cut and "Tool00" in cut and "Tool29" not in cut
    tiny = software.render(m, 300)
    assert len(tiny) <= 300 and re.search(r"\n… and \d+ more: ", tiny)


def test_a_real_message_holds_a_hundred_tools_with_summaries():
    tools = [{**M2, "name": f"Tool{i}", "area": f"Area {i % 12}"} for i in range(100)]
    text = software.render(software.parse(manifest(*tools)), 9800)
    assert text.count("- **Tool") == 100 and len(text) <= 9800


def test_load_missing_regular_fifo_large_and_not_utf8(tmp_path):
    assert software.load(str(tmp_path / "none.json")) is None
    assert software.load(write(tmp_path, manifest(M2))).tools[0].name == "Macaulay2"
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    done: list[str] = []

    def read() -> None:
        try:
            software.load(str(fifo))
        except software.ManifestError as exc:
            done.append(str(exc))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(5)
    assert done == ["not a regular file"], "a FIFO must not block !tools"
    big = manifest(M2) + " " * software.MAX_BYTES
    with pytest.raises(software.ManifestError, match="larger than"):
        software.load(write(tmp_path, big))
    with pytest.raises(software.ManifestError, match="not UTF-8"):
        software.load(write(tmp_path, b'{"schema": 1, "tools": ["\xff"]}'))
    with pytest.raises(software.ManifestError, match="not a regular file"):
        software.load(str(tmp_path))


async def test_the_reply_is_one_plain_sentence_when_the_list_is_missing_or_bad(tmp_path):
    assert await software.reply(9800, str(tmp_path / "none.json")) == software.MISSING
    assert await software.reply(9800, write(tmp_path, "{")) == software.UNREADABLE
    assert await software.reply(9800, write(tmp_path, '{"schema": 1, "tools": []}')) == (
        software.EMPTY
    )
    os.chmod(write(tmp_path, manifest(M2)), 0)
    if os.geteuid() != 0:  # root reads it anyway
        assert await software.reply(9800, str(tmp_path / "tools.json")) == software.UNREADABLE
    for text in (software.MISSING, software.UNREADABLE, software.EMPTY):
        assert text.endswith(".") and ". " not in text and "\n" not in text


@pytest.mark.parametrize("where", ["dm", "new topic", "session topic"])
async def test_tools_is_answered_by_the_bridge_without_a_turn(bridge, tmp_path, monkeypatch, where):
    b, fake, backend = bridge
    monkeypatch.setattr(software, "MANIFEST", write(tmp_path, manifest(M2)))
    if where == "session topic":
        await user_message(b, fake, "@**Test Bot** start")
    turns = backend.start_turn.await_count
    if where == "dm":
        await b.handle(
            {
                "type": "message",
                "message": {
                    "id": 999,
                    "sender_id": 20,
                    "type": "private",
                    "content": "!tools",
                    "display_recipient": [{"id": 1}, {"id": 20}],
                },
            }
        )
    else:
        await user_message(b, fake, "@**Test Bot** !tools" if where == "new topic" else "!tools")
    replies = [m for m in fake.messages.values() if "**Software on this VM**" in m["content"]]
    assert len(replies) == 1
    assert replies[0]["type"] == ("private" if where == "dm" else "stream")
    m2 = "- **Macaulay2** 1.26.05 · `M2 --script file.m2` · one short line"
    assert m2 in replies[0]["content"]
    assert backend.start_turn.await_count == turns
    assert not [m for m in fake.messages.values() if "Unknown command" in m["content"]]


async def test_a_missing_list_in_a_topic_and_another_users_tools(bridge, tmp_path, monkeypatch):
    b, fake, _ = bridge
    monkeypatch.setattr(software, "MANIFEST", str(tmp_path / "none.json"))
    await user_message(b, fake, "@**Test Bot** !tools")
    assert list(fake.messages.values())[-1]["content"] == software.MISSING
    count = len(fake.messages)
    load = []
    monkeypatch.setattr(software, "load", lambda path=None: load.append(path))
    await user_message(b, fake, "@**Test Bot** !tools", sender=21, topic="other")
    assert not load and len(fake.messages) == count + 1  # only the sender's own message


def test_tools_is_in_both_helps():
    assert "`!tools`" in HELP and "`!tools`" in DM_HELP.split("\n")[0]
