"""#651: the admin bots' notes are italics as Zulip writes them, `*…*`, never `_…_`.

Zulip's Markdown has no underscore emphasis, so `!status all` ended with
`_Retired, so left out: …._`, underscores and all. Each note under a cut table, a cut output or
a status is rendered here by the product's own code and must match Zulip's own italics rule
(`botkit.ZULIP_ITALIC`) whole, with each of its attachment's lines. The runner's two notes are
checked where its tests make them (`test_admin_runner`, `test_admin_retire`), and the
source-wide check that no `_…_` comes back is `tests/dev/test_zulip_emphasis.py`.
"""

from dataclasses import replace
from types import SimpleNamespace

from zab.admin import planning
from zab.admin.planning import ATTACH, Planning
from zab.admin.scrub import Scrubber

from .botkit import ZULIP_ITALIC
from .test_admin_actions import FLEET
from .test_admin_plan_fit import vm_rows
from .test_admin_planning import _status_plan
from .test_admin_status_reply import reply

LINK = "[P-7-plan.txt](/user_uploads/2/ab/xyzXYZ_-12/P-7-plan.txt)"


def italic(line: str) -> bool:
    return ZULIP_ITALIC.fullmatch(line) is not None


def test_the_retired_rows_line_is_italics():
    one = replace(FLEET, retired={"bob": "P-50 at 2026-10-29 14:02 UTC"})
    rows = tuple(replace(r, slug=f"row-{n:02d}") for n, r in enumerate(FLEET.rows * 3))
    many = replace(FLEET, rows=rows, retired={r.slug: "P-50" for r in rows})
    for fleet in (one, many):
        line = planning.left_out(fleet)
        assert italic(line) and line.startswith("*Retired, so left out: "), line
    assert planning.left_out(many).endswith(f", and {len(rows) - 10} more.*")


def test_a_cut_status_ends_with_italics():
    text = reply(*vm_rows([f"vm-{n:02d}" for n in range(planning.STATUS_LINES + 1)], 0)).text
    note = text.split("\n")[-1]
    assert note.startswith("*Cut to fit: ") and note.endswith(f"{ATTACH}*")
    for whole in (f"The whole status: {LINK}.", "Its attachment failed."):  # `Planning.status`
        assert italic(note.replace(ATTACH, whole)), note


def test_a_cut_plan_has_an_italic_note():
    text = _status_plan(planning.STATUS_LINES + 1, dry="vm-000: would stop\n").text
    (note,) = [line for line in text.split("\n") if "Cut to fit" in line]
    assert italic(note.replace(ATTACH, f"The whole plan: {LINK}.")), note  # `Planning.plan`


async def test_a_refusals_cut_output_has_an_italic_note():
    """`Planning._block`, under a refusal whose output is over 40 lines, attached or not."""
    for link in (LINK, None):

        async def attach(bot, name, text, link=link):
            return link

        planner = SimpleNamespace(server=SimpleNamespace(scrubber=Scrubber()), _attach=attach)
        shown = await Planning._block(planner, None, "line\n" * 50, "P-7-output.txt")
        note = shown.split("\n")[-1]
        assert note.startswith("*Cut to 40 lines") and italic(note), note
