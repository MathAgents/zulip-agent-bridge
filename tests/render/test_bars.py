"""The shared ten-cell bar of `!usage` and `!vm` (SPEC §4.6; issue #204)."""

from __future__ import annotations

import pytest

from zab.core import types as t
from zab.core.usage import render_usage
from zab.render.bars import bar, cells, whole_percent


@pytest.mark.parametrize(
    ("fraction", "color", "filled", "percent"),
    [
        (0, "🟩", 0, 0),
        (0.001, "🟩", 1, 0),
        (0.23, "🟩", 2, 23),
        (0.25, "🟩", 3, 25),
        (0.3476, "🟩", 4, 35),  # 3.476 tenths, but labelled 35%: four cells, as for 0.35
        (0.35, "🟩", 4, 35),
        (0.694, "🟩", 7, 69),
        (0.699, "🟨", 7, 70),  # labelled 70%, so not green
        (0.7, "🟨", 7, 70),
        (0.8, "🟧", 8, 80),
        (0.9, "🟥", 9, 90),
        (1, "🟥", 10, 100),
        (1.7, "🟥", 10, 100),
        (-0.2, "🟩", 0, 0),
    ],
)
def test_green_yellow_orange_red_ten_cell_bars(fraction, color, filled, percent):
    assert cells(fraction) == color * filled + "⬜" * (10 - filled)
    assert whole_percent(fraction) == percent
    assert bar(fraction) == f"{cells(fraction)} {percent}%"


@pytest.mark.parametrize("fraction,percent", [(0.123, "12%"), (0.125, "13%"), (0.999, "100%")])
def test_bar_rounds_to_whole_percent(fraction, percent):
    assert bar(fraction).split()[-1] == percent


def test_usage_limits_use_the_shared_green_bar():
    reply = render_usage(
        t.Usage(windows=(t.UsageWindow(label="Weekly", used_fraction=0.23),)), "codex", "box"
    )
    assert f"| Weekly | {bar(0.23)} |" in reply
    assert "🟩🟩⬜" in reply and "🟦" not in reply
