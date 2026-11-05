"""The status message while a turn runs (SPEC §4.9, B.2; issue #654): when it started (#754),
the agent's last notes, cut after a word, and the message budget."""

from __future__ import annotations

import random
import time

import pytest

from zab.core import presentation as view
from zab.core import types as t
from zab.render.links import ZULIP_TIME
from zab.render.markdown import MarkdownRenderer

R = MarkdownRenderer()


def item(item_id: str, status: t.ItemStatus, title: str = "python3 run.py") -> t.ItemInfo:
    return t.ItemInfo(item_id=item_id, kind=t.ItemKind.COMMAND, status=status, title=title)


def body(text: str) -> list[str]:
    return text.splitlines()[1:]


def test_nothing_changes_only_because_time_passes(monkeypatch):
    """#754: a running turn shows when it started, as a Zulip time, and a running step no time
    of its own, so a refresh with nothing new has nothing to edit."""
    started = 1_793_613_600  # 2026-11-02T10:00:00Z
    actions = [item("a", t.ItemStatus.COMPLETED, "ls"), item("b", t.ItemStatus.IN_PROGRESS)]
    text = view.status("running", started, actions, R, 10000)
    head = "⏳ **Working** · started <time:2026-11-02T10:00:00+00:00> · 2 actions · model default"
    assert text.splitlines()[0].startswith(head)
    assert body(text) == ["- ✅ ` ls `", "- ⏳ ` python3 run.py `"]
    monkeypatch.setattr(view.time, "time", lambda: started + 3 * 3600)  # three hours later
    assert view.status("running", started, actions, R, 10000) == text


def test_the_summary_keeps_the_turns_duration():
    now = time.time()
    running = [item("b", t.ItemStatus.IN_PROGRESS)]
    done = view.status("interrupted", now - 200, running, R, 10000)
    assert done.startswith("⏹ **Stopped** · 3m 20s · 1 actions") and "<time:" not in done
    assert "⏳ ` python3 run.py `" in done
    # until when it ended, however late it is rendered again (a retried answer)
    again = view.status("completed", now - 7200, running, R, 10000, ended=now - 7000)
    assert again.startswith("✅ **Done** · 3m 20s · ")


def test_the_last_three_notes_newest_last():
    notes = ["first note", "second note", "third note", "fourth note"]
    lines = body(view.status("running", time.time(), [], R, 10000, notes=notes))
    # one quote, a paragraph each
    assert lines == ["> second note", ">", "> third note", ">", "> fourth note"]
    # an empty note is none; a finished turn shows none
    assert body(view.status("running", time.time(), [], R, 10000, notes=["a", " "])) == ["> a"]
    assert "note" not in view.status("completed", time.time(), [], R, 10000, notes=notes)
    # one note as a plain string, as callers passed it before #654
    assert body(view.status("running", time.time(), [], R, 10000, (), "only")) == ["> only"]


def test_a_long_note_is_cut_after_a_word():
    words = " ".join(f"word{i:03d}" for i in range(100))  # 799 characters
    cut = view.clip(words)
    assert len(cut) <= view.NOTE_CHARS and cut.endswith("…")
    assert cut[:-1] == words[: len(cut) - 1] and words[len(cut) - 1] == " "  # whole words
    # on one line; a note that fits is kept whole
    assert view.clip("two\n\nparagraphs,  spaced") == "two paragraphs, spaced"
    assert view.clip("x" * 500) == "x" * 500
    # only a word longer than the limit is cut inside it
    assert view.clip("y" * 600) == "y" * 499 + "…"
    line = body(view.status("running", time.time(), [], R, 10000, notes=[words]))[0]
    assert line == "> " + cut


STAMP = "<time:2026-10-27T01:08:38Z>"  # Zulip's timestamp, in each reader's time zone


def test_a_zulip_time_is_never_cut_in_half():
    """Issue #673: half of a `<time:…>` shows as raw text, so a cut that would fall inside one
    goes before it, whether it falls after a word or inside a word longer than the limit."""
    # the stamp straddles the limit: the cut after a word drops it whole
    assert view.clip("w" * 480 + f" at {STAMP} then more") == "w" * 480 + " at…"
    # a stamp written with spaces, which Zulip takes to its `>`: not cut at a space inside it
    spaced = "w" * 470 + " at <time:2026-10-27 01:08:38 UTC> then"
    assert view.clip(spaced) == "w" * 470 + " at…"
    # a single word longer than the limit: cut before the stamp, not inside it
    assert view.clip("w" * 490 + STAMP + "x" * 20) == "w" * 490 + "…"
    # a stamp is one word: when the only space is inside one at the start, the cut goes inside
    # the word after it, not to nothing (the review of #676)
    first = "<time:2026-10-27 01:08:38Z>"
    assert view.clip(first + "x" * 600) == first + "x" * 472 + "…"
    # a stamp before the cut is kept
    kept = view.clip(f"{STAMP} " + "word " * 120)
    assert kept.startswith(f"{STAMP} word ") and kept.endswith("word…")
    # and the status message shows the stamp as written, cut or not
    notes = [f"Started {STAMP}; waiting.", spaced]
    lines = body(view.status("running", time.time(), [], R, 10000, notes=notes))
    assert lines == [f"> Started {STAMP}; waiting.", ">", "> " + "w" * 470 + " at…"]
    # a note of `<time:` that never closes is scanned in linear time (CPU time)
    start = time.process_time()
    assert view.clip("<time:" * 30_000) == ("<time:" * 84)[:499] + "…"
    assert time.process_time() - start < 1.0  # unbounded, it took about 12 s


def test_a_cut_note_is_a_prefix_within_the_limit_with_no_time_cut_open():
    """Issue #673, the review of #676: on random notes of words, spaced and unclosed times and
    long runs, `clip` stays within the limit, is a prefix of the note on one line plus `…`,
    and no Zulip time of that line straddles the cut."""
    rng = random.Random(673)  # noqa: S311 - reproducible cases, not secrets
    pieces = [STAMP, "<time:2026-10-27 01:08:38 UTC>", "<time:", "x" * 120, "word", " ", "\n"]
    for _ in range(3000):
        limit = rng.randint(5, 500)
        note = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 60)))
        line, cut = " ".join(note.split()), view.clip(note, limit)
        assert len(cut) <= limit
        if cut == line:
            continue
        kept = cut[:-1]
        assert cut.endswith("…") and line.startswith(kept)
        assert not any(m.start() < len(kept) < m.end() for m in ZULIP_TIME.finditer(line))


def test_the_budget_drops_whole_lines_and_keeps_the_newest_note():
    now = time.time()
    actions = [item(str(i), t.ItemStatus.COMPLETED, f"step-{i}") for i in range(8)]
    actions.append(item("run", t.ItemStatus.IN_PROGRESS, "sleep 300"))
    notes = ["older " * 60, "old " * 60, "the newest note"]
    full = view.status("running", now, actions, R, 10000, notes=notes)
    header = full.splitlines()[0]
    budget = 300  # what the status may hold below its header
    text = view.status("running", now, actions, R, len(header) + 300 + budget, notes=notes)
    lines = body(text)
    assert len("\n".join(lines)) <= budget
    assert lines[-1] == "> the newest note"
    assert all(line in body(full) or line.startswith("- ⏳ ` sleep 300 `") for line in lines)
    assert not any(line.startswith("- ✅ ` step-0 `") for line in lines)  # the oldest went first


KEY = "A" * 8 + "b" * 8 + "C" * 8 + "d" * 8  # 32 alphanumerics: a fake zuliprc key, a decoy


@pytest.mark.parametrize("sep", ["", "\x00"])
def test_a_note_is_redacted_before_it_is_put_on_one_line(sep):
    """Review of #658: the zuliprc rule matches a whole line `key=…` (SPEC §11.6), so a note is
    redacted before `clip` joins its lines; a NUL inside the key is dropped first, as
    `render_inline` does. The status message is edited, and Zulip keeps every edit."""
    key = KEY[:16] + sep + KEY[16:]
    zuliprc = f"Found the bot's config:\n[api]\nemail=bot@example.org\nkey={key}\nsite=https://x"
    pem = "and\n-----BEGIN PRIVATE KEY-----\nMIIEvQ\nIBADAN\n-----END PRIVATE KEY-----\nthen"
    text = view.status("running", time.time(), [], R, 10000, notes=[zuliprc, pem])
    assert KEY not in text and KEY[:16] not in text and "MIIEvQ" not in text
    first, _, second = body(text)
    assert first.endswith("email=bot@example.org [redacted zulip api key] site=https://x")
    assert second == "> and [redacted private key] then"


def test_no_empty_quote_line_when_the_budget_drops_the_first_note():
    notes = ["first " * 40, "second", "third"]
    header = view.status("running", time.time(), [], R, 10000).splitlines()[0]
    text = view.status("running", time.time(), [], R, len(header) + 300 + 20, notes=notes)
    assert body(text) == ["> second", ">", "> third"]
